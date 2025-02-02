#!/usr/bin/env python
"""Tests for frontend server, client communicator, and the GRRHTTPClient."""

import array
import datetime
import logging
import pdb
import time
from unittest import mock
import zlib

from absl import app
from absl import flags
from absl.testing import absltest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
import requests

from google.protobuf import wrappers_pb2
from grr_response_client import comms
from grr_response_client.client_actions import admin
from grr_response_client.client_actions import standard
from grr_response_core import config
from grr_response_core.lib import queues
from grr_response_core.lib import rdfvalue
from grr_response_core.lib import utils
from grr_response_core.lib.rdfvalues import client as rdf_client
from grr_response_core.lib.rdfvalues import crypto as rdf_crypto
from grr_response_core.lib.rdfvalues import flows as rdf_flows
from grr_response_core.lib.rdfvalues import protodict as rdf_protodict
from grr_response_server import communicator
from grr_response_server import data_store
from grr_response_server import fleetspeak_connector
from grr_response_server import flow
from grr_response_server import flow_base
from grr_response_server import frontend_lib
from grr_response_server import maintenance_utils
from grr_response_server import sinks
from grr_response_server.databases import db as abstract_db
from grr_response_server.databases import db_test_utils
from grr_response_server.flows.general import administrative
from grr_response_server.flows.general import ca_enroller
from grr_response_server.rdfvalues import flow_objects as rdf_flow_objects
from grr_response_server.rdfvalues import objects as rdf_objects
from grr_response_server.sinks import test_lib as sinks_test_lib
from grr.test_lib import client_action_test_lib
from grr.test_lib import client_test_lib
from grr.test_lib import db_test_lib
from grr.test_lib import flow_test_lib
from grr.test_lib import stats_test_lib
from grr.test_lib import test_lib
from grr.test_lib import worker_mocks
from grr_response_proto import rrg_pb2


class SendingTestFlow(flow_base.FlowBase):
  """Tests that sent messages are correctly collected."""

  def Start(self):
    for i in range(10):
      self.CallClient(
          client_test_lib.Test,
          rdf_protodict.DataBlob(string="test%s" % i),
          data=str(i),
          next_state="Incoming")


MESSAGE_EXPIRY_TIME = 100


def ReceiveMessages(client_id, messages):
  server = TestServer()
  server.ReceiveMessages(client_id, messages)


def TestServer():
  return frontend_lib.FrontEndServer(
      certificate=config.CONFIG["Frontend.certificate"],
      private_key=config.CONFIG["PrivateKeys.server_key"],
      message_expiry_time=MESSAGE_EXPIRY_TIME)


class GRRFEServerTestRelational(flow_test_lib.FlowTestsBaseclass):
  """Tests the GRRFEServer with relational flows enabled."""

  def _FlowSetup(self, client_id, flow_id):
    rdf_flow = rdf_flow_objects.Flow(
        flow_class_name=administrative.OnlineNotification.__name__,
        client_id=client_id,
        flow_id=flow_id,
        create_time=rdfvalue.RDFDatetime.Now())
    data_store.REL_DB.WriteFlowObject(rdf_flow)

    req = rdf_flow_objects.FlowRequest(
        client_id=client_id, flow_id=flow_id, request_id=1)

    data_store.REL_DB.WriteFlowRequests([req])

    return rdf_flow, req

  def testReceiveMessages(self):
    """Tests receiving messages."""
    client_id = "C.1234567890123456"
    flow_id = "12345678"
    data_store.REL_DB.WriteClientMetadata(client_id, fleetspeak_enabled=False)
    _, req = self._FlowSetup(client_id, flow_id)

    session_id = "%s/%s" % (client_id, flow_id)
    messages = [
        rdf_flows.GrrMessage(
            request_id=1,
            response_id=i,
            session_id=session_id,
            auth_state="AUTHENTICATED",
            payload=rdfvalue.RDFInteger(i)) for i in range(1, 10)
    ]

    ReceiveMessages(client_id, messages)
    received = data_store.REL_DB.ReadAllFlowRequestsAndResponses(
        client_id, flow_id)
    self.assertLen(received, 1)
    self.assertEqual(received[0][0], req)
    self.assertLen(received[0][1], 9)

  def testBlobHandlerMessagesAreHandledOnTheFrontend(self):
    client_id = "C.1234567890123456"
    data_store.REL_DB.WriteClientMetadata(client_id, fleetspeak_enabled=False)

    # Check that the worker queue is empty.
    self.assertEmpty(data_store.REL_DB.ReadMessageHandlerRequests())

    data = b"foo"
    data_blob = rdf_protodict.DataBlob(
        data=zlib.compress(data),
        compression=rdf_protodict.DataBlob.CompressionType.ZCOMPRESSION)
    messages = [
        rdf_flows.GrrMessage(
            source=client_id,
            session_id=str(rdfvalue.SessionID(flow_name="TransferStore")),
            payload=data_blob,
            auth_state=rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED,
        )
    ]
    ReceiveMessages(client_id, messages)

    # Check that the worker queue is still empty.
    self.assertEmpty(data_store.REL_DB.ReadMessageHandlerRequests())

    # Check that the blob was written to the blob store.
    self.assertTrue(
        data_store.BLOBS.CheckBlobExists(rdf_objects.BlobID.FromBlobData(data)))

  def testCrashReport(self):
    client_id = "C.1234567890123456"
    flow_id = "12345678"
    data_store.REL_DB.WriteClientMetadata(client_id, fleetspeak_enabled=False)
    self._FlowSetup(client_id, flow_id)

    # Make sure the event handler is present.
    self.assertTrue(administrative.ClientCrashHandler)

    session_id = "%s/%s" % (client_id, flow_id)
    status = rdf_flows.GrrStatus(
        status=rdf_flows.GrrStatus.ReturnedStatus.CLIENT_KILLED)
    messages = [
        rdf_flows.GrrMessage(
            source=client_id,
            request_id=1,
            response_id=1,
            session_id=session_id,
            payload=status,
            auth_state="AUTHENTICATED",
            type=rdf_flows.GrrMessage.Type.STATUS)
    ]

    ReceiveMessages(client_id, messages)

    crash_details_rel = data_store.REL_DB.ReadClientCrashInfo(client_id)
    self.assertTrue(crash_details_rel)
    self.assertEqual(crash_details_rel.session_id, session_id)

  def testDrainTaskSchedulerQueue(self):
    client_id = u"C.1234567890123456"
    flow_id = flow.RandomFlowId()
    data_store.REL_DB.WriteClientMetadata(client_id, fleetspeak_enabled=False)

    rdf_flow = rdf_flow_objects.Flow(
        client_id=client_id,
        flow_id=flow_id,
        create_time=rdfvalue.RDFDatetime.Now())
    data_store.REL_DB.WriteFlowObject(rdf_flow)

    action_requests = []
    for i in range(3):
      data_store.REL_DB.WriteFlowRequests([
          rdf_flow_objects.FlowRequest(
              client_id=client_id, flow_id=flow_id, request_id=i)
      ])

      action_requests.append(
          rdf_flows.ClientActionRequest(
              client_id=client_id,
              flow_id=flow_id,
              request_id=i,
              action_identifier="WmiQuery"))

    data_store.REL_DB.WriteClientActionRequests(action_requests)
    server = TestServer()

    res = server.DrainTaskSchedulerQueueForClient(client_id)
    msgs = [
        rdf_flow_objects.GRRMessageFromClientActionRequest(r)
        for r in action_requests
    ]
    for r in res:
      r.task_id = 0
    for m in msgs:
      m.task_id = 0

    self.assertCountEqual(res, msgs)


class FleetspeakFrontendTests(flow_test_lib.FlowTestsBaseclass):

  def testFleetspeakEnrolment(self):
    client_id = "C.0000000000000000"
    server = TestServer()
    # An Enrolment flow should start inline and attempt to send at least
    # message through fleetspeak as part of the resulting interrogate flow.
    with mock.patch.object(fleetspeak_connector, "CONN") as mock_conn:
      server.EnrolFleetspeakClient(client_id)
      mock_conn.outgoing.InsertMessage.assert_called()


def MakeHTTPException(code=500, msg="Error"):
  """A helper for creating a HTTPError exception."""
  response = requests.Response()
  response.status_code = code
  return requests.ConnectionError(msg, response=response)


def MakeResponse(code=500, data=""):
  """A helper for creating a HTTPError exception."""
  response = requests.Response()
  response.status_code = code
  response._content = data
  return response


class ClientCommsTest(stats_test_lib.StatsTestMixin,
                      client_action_test_lib.WithAllClientActionsMixin,
                      test_lib.GRRBaseTest):
  """Test the communicator."""

  def setUp(self):
    """Set up communicator tests."""
    super().setUp()

    # These tests change the config so we preserve state.
    config_stubber = test_lib.PreserveConfig()
    config_stubber.Start()
    self.addCleanup(config_stubber.Stop)

    self.client_private_key = config.CONFIG["Client.private_key"]

    self.server_certificate = config.CONFIG["Frontend.certificate"]
    self.server_private_key = config.CONFIG["PrivateKeys.server_key"]
    self.client_communicator = comms.ClientCommunicator(
        private_key=self.client_private_key)

    self.client_communicator.LoadServerCertificate(
        server_certificate=self.server_certificate,
        ca_certificate=config.CONFIG["CA.certificate"])

    self.last_urlmock_error = None

    self._SetupCommunicator()

  def _SetupCommunicator(self):
    self.server_communicator = frontend_lib.ServerCommunicator(
        certificate=self.server_certificate,
        private_key=self.server_private_key)

  def ClientServerCommunicate(self, timestamp=None):
    """Tests the end to end encrypted communicators."""
    message_list = rdf_flows.MessageList()
    for i in range(1, 11):
      message_list.job.Append(
          session_id=rdfvalue.SessionID(
              base="aff4:/flows", queue=queues.FLOWS, flow_name=i),
          name="OMG it's a string")

    result = rdf_flows.ClientCommunication()
    timestamp = self.client_communicator.EncodeMessages(
        message_list, result, timestamp=timestamp)
    self.cipher_text = result.SerializeToBytes()

    (decoded_messages, source, client_timestamp) = (
        self.server_communicator.DecryptMessage(self.cipher_text))

    self.assertEqual(source, self.client_communicator.common_name)
    self.assertEqual(client_timestamp, timestamp)
    self.assertLen(decoded_messages, 10)
    for i in range(1, 11):
      self.assertEqual(
          decoded_messages[i - 1].session_id,
          rdfvalue.SessionID(
              base="aff4:/flows", queue=queues.FLOWS, flow_name=i))

    return decoded_messages

  def testCommunications(self):
    """Test that messages from unknown clients are tagged unauthenticated."""
    decoded_messages = self.ClientServerCommunicate()
    for i in range(len(decoded_messages)):
      self.assertEqual(decoded_messages[i].auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.UNAUTHENTICATED)

  def _MakeClientRecord(self):
    """Make a client in the data store."""
    client_cert = self.ClientCertFromPrivateKey(self.client_private_key)
    self.client_id = client_cert.GetCN()[len("aff4:/"):]
    data_store.REL_DB.WriteClientMetadata(
        self.client_id, fleetspeak_enabled=False, certificate=client_cert)

  def testKnownClient(self):
    """Test that messages from known clients are authenticated."""
    self._MakeClientRecord()

    # Now the server should know about it
    decoded_messages = self.ClientServerCommunicate()

    for i in range(len(decoded_messages)):
      self.assertEqual(decoded_messages[i].auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

  def testClientPingAndClockIsUpdated(self):
    """Check PING and CLOCK are updated."""
    self._MakeClientRecord()

    now = rdfvalue.RDFDatetime.Now()
    client_now = now - 20
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=client_now)

    metadata = data_store.REL_DB.ReadClientMetadata(self.client_id)

    self.assertEqual(now, metadata.ping)
    self.assertEqual(client_now, metadata.clock)

    now += 60
    client_now += 40
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=client_now)

    metadata = data_store.REL_DB.ReadClientMetadata(self.client_id)
    self.assertEqual(now, metadata.ping)
    self.assertEqual(client_now, metadata.clock)

  def testClientPingStatsUpdated(self):
    """Check client ping stats are updated."""
    self._MakeClientRecord()

    data_store.REL_DB.WriteGRRUser("Test")

    with self.assertStatsCounterDelta(
        1, frontend_lib.CLIENT_PINGS_BY_LABEL, fields=["testlabel"]):

      data_store.REL_DB.AddClientLabels(self.client_id, "Test", ["testlabel"])

      now = rdfvalue.RDFDatetime.Now()
      with test_lib.FakeTime(now):
        self.ClientServerCommunicate(timestamp=now)

  def testServerReplayAttack(self):
    """Test that replaying encrypted messages to the server invalidates them."""
    self._MakeClientRecord()

    # First send some messages to the server
    decoded_messages = self.ClientServerCommunicate(timestamp=1000000)

    encrypted_messages = self.cipher_text

    self.assertEqual(decoded_messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Immediate replay is accepted by the server since some proxies do this.
    (decoded_messages, _,
     _) = self.server_communicator.DecryptMessage(encrypted_messages)

    self.assertEqual(decoded_messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Move the client time more than 1h forward.
    self.ClientServerCommunicate(timestamp=1000000 + 3700 * 1000000)

    # And replay the old messages again.
    (decoded_messages, _,
     _) = self.server_communicator.DecryptMessage(encrypted_messages)

    # Messages should now be tagged as desynced
    self.assertEqual(decoded_messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.DESYNCHRONIZED)

  def testX509Verify(self):
    """X509 Verify can have several failure paths."""

    # This is a successful verify.
    with mock.patch.object(
        rdf_crypto.RDFX509Cert, "Verify", lambda self, public_key=None: True):
      self.client_communicator.LoadServerCertificate(
          self.server_certificate, config.CONFIG["CA.certificate"])

    def Verify(_, public_key=False):
      _ = public_key
      raise rdf_crypto.VerificationError("Testing verification failure.")

    # Mock the verify function to simulate certificate failures.
    with mock.patch.object(rdf_crypto.RDFX509Cert, "Verify", Verify):
      self.assertRaises(IOError, self.client_communicator.LoadServerCertificate,
                        self.server_certificate,
                        config.CONFIG["CA.certificate"])

  def testErrorDetection(self):
    """Tests the end to end encrypted communicators."""
    # Install the client - now we can verify its signed messages
    self._MakeClientRecord()

    # Make something to send
    message_list = rdf_flows.MessageList()
    for i in range(0, 10):
      message_list.job.Append(session_id=str(i))

    result = rdf_flows.ClientCommunication()
    self.client_communicator.EncodeMessages(message_list, result)

    cipher_text = result.SerializeToBytes()

    # Depending on this modification several things may happen:
    # 1) The padding may not match which will cause a decryption exception.
    # 2) The protobuf may fail to decode causing a decoding exception.
    # 3) The modification may affect the signature resulting in UNAUTHENTICATED
    #    messages.
    # 4) The modification may have no effect on the data at all.
    for x in range(0, len(cipher_text), 50):
      # Futz with the cipher text (Make sure it's really changed)
      mod = chr((cipher_text[x] % 250) + 1).encode("latin-1")
      mod_cipher_text = cipher_text[:x] + mod + cipher_text[x + 1:]

      try:
        decoded, client_id, _ = self.server_communicator.DecryptMessage(
            mod_cipher_text)

        for i, message in enumerate(decoded):
          # If the message is actually authenticated it must not be changed!
          if message.auth_state == message.AuthorizationState.AUTHENTICATED:
            self.assertEqual(message.source, client_id)

            # These fields are set by the decoder and are not present in the
            # original message - so we clear them before comparison.
            message.auth_state = None
            message.source = None
            self.assertEqual(message, message_list.job[i])
          else:
            logging.debug("Message %s: Authstate: %s", i, message.auth_state)

      except communicator.DecodingError as e:
        logging.debug("Detected alteration at %s: %s", x, e)

  def testEnrollingCommunicator(self):
    """Test that the ClientCommunicator generates good keys."""
    self.client_communicator = comms.ClientCommunicator()

    self.client_communicator.LoadServerCertificate(
        self.server_certificate, config.CONFIG["CA.certificate"])

    # Verify that the CN is of the correct form
    csr = self.client_communicator.GetCSR()
    cn = rdf_client.ClientURN.FromPublicKey(csr.GetPublicKey())
    self.assertEqual(cn, csr.GetCN())

  def testServerKeyRotation(self):
    self._MakeClientRecord()

    # Now the server should know about the client.
    decoded_messages = self.ClientServerCommunicate()
    for i in range(len(decoded_messages)):
      self.assertEqual(decoded_messages[i].auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Suppress the output.
    with mock.patch.object(maintenance_utils, "EPrint", lambda msg: None):
      maintenance_utils.RotateServerKey()

    server_certificate = config.CONFIG["Frontend.certificate"]
    server_private_key = config.CONFIG["PrivateKeys.server_key"]

    self.assertNotEqual(server_certificate, self.server_certificate)
    self.assertNotEqual(server_private_key, self.server_private_key)

    self.server_communicator = frontend_lib.ServerCommunicator(
        certificate=server_certificate, private_key=server_private_key)

    # Clients can't connect at this point since they use the outdated
    # session key.
    with self.assertRaises(communicator.DecryptionError):
      self.ClientServerCommunicate()

    # After the client reloads the server cert, this should start
    # working again.
    self.client_communicator.LoadServerCertificate(
        server_certificate=server_certificate,
        ca_certificate=config.CONFIG["CA.certificate"])

    self.assertLen(list(self.ClientServerCommunicate()), 10)


class HTTPClientTests(client_action_test_lib.WithAllClientActionsMixin,
                      test_lib.GRRBaseTest):
  """Test the http communicator."""

  def setUp(self):
    """Set up communicator tests."""
    super().setUp()

    # These tests change the config so we preserve state.
    config_stubber = test_lib.PreserveConfig()
    config_stubber.Start()
    self.addCleanup(config_stubber.Stop)

    self.server_private_key = config.CONFIG["PrivateKeys.server_key"]
    self.server_certificate = config.CONFIG["Frontend.certificate"]

    # Make a new client
    self.CreateNewClientObject()

    # And cache it in the server
    self.CreateNewServerCommunicator()

    requests_stubber = mock.patch.object(requests, "request", self.UrlMock)
    requests_stubber.start()
    self.addCleanup(requests_stubber.stop)

    sleep_stubber = mock.patch.object(time, "sleep", lambda x: None)
    sleep_stubber.start()
    self.addCleanup(sleep_stubber.stop)

    self.messages = []

    ca_enroller.enrolment_cache.Flush()

    # Response to send back to clients.
    self.server_response = dict(
        session_id="aff4:/W:session", name="Echo", response_id=2)

  def _MakeClient(self):
    self.client_certificate = self.ClientCertFromPrivateKey(
        config.CONFIG["Client.private_key"])
    self.client_cn = self.client_certificate.GetCN()
    self.client_id = self.client_cn[len("aff4:/"):]

    data_store.REL_DB.WriteClientMetadata(
        self.client_id,
        certificate=self.client_certificate,
        fleetspeak_enabled=False)

  def _ClearClient(self):
    del data_store.REL_DB.delegate.metadatas[self.client_id]

  def CreateNewServerCommunicator(self):
    self._MakeClient()
    self.server_communicator = frontend_lib.ServerCommunicator(
        certificate=self.server_certificate,
        private_key=self.server_private_key)

  def CreateClientCommunicator(self):
    self.client_communicator = comms.GRRHTTPClient(
        ca_cert=config.CONFIG["CA.certificate"],
        worker_cls=worker_mocks.ClientWorker)

  def CreateNewClientObject(self):
    self.CreateClientCommunicator()

    # Disable stats collection for tests.
    self.client_communicator.client_worker.last_stats_sent_time = (
        time.time() + 3600)

    # Build a client context with preloaded server certificates
    self.client_communicator.communicator.LoadServerCertificate(
        self.server_certificate, config.CONFIG["CA.certificate"])

    self.client_communicator.http_manager.retry_error_limit = 5

  def UrlMock(self, num_messages=10, url=None, data=None, **kwargs):
    """A mock for url handler processing from the server's POV."""
    if "server.pem" in url:
      cert = str(config.CONFIG["Frontend.certificate"]).encode("ascii")
      return MakeResponse(200, cert)

    _ = kwargs
    try:
      comms_cls = rdf_flows.ClientCommunication
      self.client_communication = comms_cls.FromSerializedBytes(data)

      # Decrypt incoming messages
      self.messages, source, ts = self.server_communicator.DecodeMessages(
          self.client_communication)

      # Make sure the messages are correct
      self.assertEqual(source, self.client_cn)
      messages = sorted(
          [m for m in self.messages if m.session_id == "aff4:/W:session"],
          key=lambda m: m.response_id)
      self.assertEqual([m.response_id for m in messages],
                       list(range(len(messages))))
      self.assertEqual([m.request_id for m in messages], [1] * len(messages))

      # Now prepare a response
      response_comms = rdf_flows.ClientCommunication()
      message_list = rdf_flows.MessageList()
      for i in range(0, num_messages):
        message_list.job.Append(request_id=i, **self.server_response)

      # Preserve the timestamp as a nonce
      self.server_communicator.EncodeMessages(
          message_list,
          response_comms,
          destination=source,
          timestamp=ts,
          api_version=self.client_communication.api_version)

      return MakeResponse(200, response_comms.SerializeToBytes())
    except communicator.UnknownClientCertError:
      raise MakeHTTPException(406)
    except Exception as e:
      logging.info("Exception in mock urllib.request.Open: %s.", e)
      self.last_urlmock_error = e

      if flags.FLAGS.pdb_post_mortem:
        pdb.post_mortem()

      raise MakeHTTPException(500)

  def CheckClientQueue(self):
    """Checks that the client context received all server messages."""
    # Check the incoming messages
    self.assertEqual(self.client_communicator.client_worker.InQueueSize(), 10)

    for i, message in enumerate(
        self.client_communicator.client_worker._in_queue.queue):
      # This is the common name embedded in the certificate.
      self.assertEqual(message.source, "aff4:/GRR Test Server")
      self.assertEqual(message.response_id, 2)
      self.assertEqual(message.request_id, i)
      self.assertEqual(message.session_id, "aff4:/W:session")
      self.assertEqual(message.auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Clear the queue
    self.client_communicator.client_worker._in_queue.queue.clear()

  def SendToServer(self):
    """Schedule some packets from client to server."""
    # Generate some client traffic
    for i in range(0, 10):
      self.client_communicator.client_worker.SendReply(
          rdf_flows.GrrStatus(),
          session_id=rdfvalue.SessionID("W:session"),
          response_id=i,
          request_id=1)

  def testInitialEnrollment(self):
    """If the client has no certificate initially it should enroll."""

    # Clear the certificate so we can generate a new one.
    with test_lib.ConfigOverrider({
        "Client.private_key": "",
    }):
      self.CreateNewClientObject()

      # Client should get a new Common Name.
      self.assertNotEqual(self.client_cn,
                          self.client_communicator.communicator.common_name)

      self.client_cn = self.client_communicator.communicator.common_name

      # The client will sleep and re-attempt to connect multiple times.
      status = self.client_communicator.RunOnce()

      self.assertEqual(status.code, 406)

      # The client should now send an enrollment request.
      self.client_communicator.RunOnce()

      # Client should generate enrollment message by itself.
      self.assertLen(self.messages, 1)
      self.assertEqual(self.messages[0].session_id.Basename(),
                       "E:%s" % ca_enroller.EnrolmentHandler.handler_name)

  def testEnrollment(self):
    """Test the http response to unknown clients."""

    self._ClearClient()

    # Now communicate with the server.
    self.SendToServer()
    status = self.client_communicator.RunOnce()

    # We expect to receive a 406 and all client messages will be tagged as
    # UNAUTHENTICATED.
    self.assertEqual(status.code, 406)
    self.assertLen(self.messages, 10)
    self.assertEqual(self.messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.UNAUTHENTICATED)

    # The next request should be an enrolling request.
    self.client_communicator.RunOnce()

    self.assertLen(self.messages, 11)
    enrolment_messages = []

    expected_id = "E:%s" % ca_enroller.EnrolmentHandler.handler_name
    for m in self.messages:
      if m.session_id.Basename() == expected_id:
        enrolment_messages.append(m)

    self.assertLen(enrolment_messages, 1)

    # Now we manually run the enroll well known flow with the enrollment
    # request. This will start a new flow for enrolling the client, sign the
    # cert and add it to the data store.
    handler = ca_enroller.EnrolmentHandler()
    req = rdf_objects.MessageHandlerRequest(
        client_id=self.client_id, request=enrolment_messages[0].payload)
    handler.ProcessMessages([req])

    # The next client communication should be enrolled now.
    status = self.client_communicator.RunOnce()

    self.assertEqual(status.code, 200)

    # There should be a cert for the client right now.
    md = data_store.REL_DB.ReadClientMetadata(self.client_id)
    self.assertTrue(md.certificate)

    # Now communicate with the server once again.
    self.SendToServer()
    status = self.client_communicator.RunOnce()

    self.assertEqual(status.code, 200)

  def testEnrollmentHandler(self):
    self._ClearClient()

    # First 406 queues an EnrolmentRequest.
    status = self.client_communicator.RunOnce()
    self.assertEqual(status.code, 406)

    # Send it to the server.
    status = self.client_communicator.RunOnce()
    self.assertEqual(status.code, 406)

    self.assertLen(self.messages, 1)
    self.assertEqual(self.messages[0].session_id.Basename(),
                     "E:%s" % ca_enroller.EnrolmentHandler.handler_name)

    request = rdf_objects.MessageHandlerRequest(
        client_id=self.messages[0].source.Basename(),
        handler_name="Enrol",
        request_id=12345,
        request=self.messages[0].payload)

    handler = ca_enroller.EnrolmentHandler()
    handler.ProcessMessages([request])

    # The next client communication should give a 200.
    status = self.client_communicator.RunOnce()
    self.assertEqual(status.code, 200)

  def testReboots(self):
    """Test the http communication with reboots."""
    # Now we add the new client record to the server cache
    self.SendToServer()
    self.client_communicator.RunOnce()
    self.CheckClientQueue()

    # Simulate the client rebooted
    self.CreateNewClientObject()

    self.SendToServer()
    self.client_communicator.RunOnce()
    self.CheckClientQueue()

    # Simulate the server rebooting
    self.CreateNewServerCommunicator()

    self.SendToServer()
    self.client_communicator.RunOnce()
    self.CheckClientQueue()

  def _CheckFastPoll(self, require_fastpoll, expected_sleeptime):
    self.server_response = dict(
        session_id="aff4:/W:session",
        name="Echo",
        response_id=2,
        require_fastpoll=require_fastpoll)

    # Make sure we don't have any output messages that might override the
    # fastpoll setting from the input messages we send
    self.assertEqual(self.client_communicator.client_worker.OutQueueSize(), 0)

    self.client_communicator.RunOnce()
    # Make sure the timer is set to the correct value.
    self.assertEqual(self.client_communicator.timer.sleep_time,
                     expected_sleeptime)
    self.CheckClientQueue()

  def testNoFastPoll(self):
    """Test that the fast poll False is respected on input messages.

    Also make sure we wait the correct amount of time before next poll.
    """
    self._CheckFastPoll(False, config.CONFIG["Client.poll_max"])

  def testFastPoll(self):
    """Test that the fast poll True is respected on input messages.

    Also make sure we wait the correct amount of time before next poll.
    """
    self._CheckFastPoll(True, config.CONFIG["Client.poll_min"])

  def testCorruption(self):
    """Simulate corruption of the http payload."""

    self.corruptor_field = None

    def Corruptor(url="", data=None, **kwargs):
      """Futz with some of the fields."""
      comm_cls = rdf_flows.ClientCommunication
      if data is not None:
        self.client_communication = comm_cls.FromSerializedBytes(data)
      else:
        self.client_communication = comm_cls(None)

      if self.corruptor_field and "server.pem" not in url:
        orig_str_repr = self.client_communication.SerializeToBytes()
        field_data = getattr(self.client_communication, self.corruptor_field)
        if hasattr(field_data, "SerializeToBytes"):
          # This converts encryption keys to a string so we can corrupt them.
          field_data = field_data.SerializeToBytes()

        modified_data = array.array("B", field_data)
        offset = len(field_data) // 2
        char = field_data[offset]
        modified_data[offset] = char % 250 + 1
        setattr(self.client_communication, self.corruptor_field,
                modified_data.tobytes())

        # Make sure we actually changed the data.
        self.assertNotEqual(field_data, modified_data)

        mod_str_repr = self.client_communication.SerializeToBytes()
        self.assertLen(orig_str_repr, len(mod_str_repr))
        differences = [
            True for x, y in zip(orig_str_repr, mod_str_repr) if x != y
        ]
        self.assertLen(differences, 1)

      data = self.client_communication.SerializeToBytes()
      return self.UrlMock(url=url, data=data, **kwargs)

    with mock.patch.object(requests, "request", Corruptor):
      self.SendToServer()
      status = self.client_communicator.RunOnce()
      self.assertEqual(status.code, 200)

      for field in ["packet_iv", "encrypted"]:
        # Corrupting each field should result in HMAC verification errors.
        self.corruptor_field = field

        self.SendToServer()
        status = self.client_communicator.RunOnce()

        self.assertEqual(status.code, 500)
        self.assertIn("HMAC verification failed", str(self.last_urlmock_error))

      # Corruption of these fields will likely result in RSA errors, since we do
      # the RSA operations before the HMAC verification (in order to recover the
      # hmac key):
      for field in ["encrypted_cipher", "encrypted_cipher_metadata"]:
        # Corrupting each field should result in HMAC verification errors.
        self.corruptor_field = field

        self.SendToServer()
        status = self.client_communicator.RunOnce()

        self.assertEqual(status.code, 500)

  def testClientRetransmission(self):
    """Test that client retransmits failed messages."""
    fail = True
    num_messages = 10

    def FlakyServer(url=None, **kwargs):
      if not fail or "server.pem" in url:
        return self.UrlMock(num_messages=num_messages, url=url, **kwargs)

      raise MakeHTTPException(500)

    with mock.patch.object(requests, "request", FlakyServer):
      self.SendToServer()
      status = self.client_communicator.RunOnce()
      self.assertEqual(status.code, 500)

      # Server should not receive anything.
      self.assertEmpty(self.messages)

      # Try to send these messages again.
      fail = False

      self.assertEqual(self.client_communicator.client_worker.InQueueSize(), 0)

      status = self.client_communicator.RunOnce()

      self.assertEqual(status.code, 200)

      # We have received 10 client messages.
      self.assertEqual(self.client_communicator.client_worker.InQueueSize(), 10)
      self.CheckClientQueue()

      # Server should have received 10 messages this time.
      self.assertLen(self.messages, 10)

  # TODO(hanuszczak): We have a separate test suite for the stat collector.
  # Most of these test methods are no longer required, especially that now they
  # need to use implementation-specific methods instead of the public API.

  def testClientStatsCollection(self):
    """Tests that the client stats are collected automatically."""
    now = 1000000
    # Pretend we have already sent stats.
    self.client_communicator.client_worker.stats_collector._last_send_time = (
        rdfvalue.RDFDatetime.FromSecondsSinceEpoch(now))

    with test_lib.FakeTime(now):
      self.client_communicator.client_worker.stats_collector._Send()

    runs = []
    with mock.patch.object(admin.GetClientStatsAuto, "Run",
                           lambda cls, _: runs.append(1)):

      # No stats collection after 10 minutes.
      with test_lib.FakeTime(now + 600):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEmpty(runs)

      # Let one hour pass.
      with test_lib.FakeTime(now + 3600):
        self.client_communicator.client_worker.stats_collector._Send()
        # This time the client should collect stats.
        self.assertLen(runs, 1)

      # Let one hour and ten minutes pass.
      with test_lib.FakeTime(now + 3600 + 600):
        self.client_communicator.client_worker.stats_collector._Send()
        # Again, there should be no stats collection, as last collection
        # happened less than an hour ago.
        self.assertLen(runs, 1)

  def testClientStatsCollectionHappensEveryMinuteWhenClientIsBusy(self):
    """Tests that client stats are collected more often when client is busy."""
    now = 1000000
    # Pretend we have already sent stats.
    self.client_communicator.client_worker.stats_collector._last_send_time = (
        rdfvalue.RDFDatetime.FromSecondsSinceEpoch(now))
    self.client_communicator.client_worker._is_active = True

    with test_lib.FakeTime(now):
      self.client_communicator.client_worker.stats_collector._Send()

    runs = []
    with mock.patch.object(admin.GetClientStatsAuto, "Run",
                           lambda cls, _: runs.append(1)):

      # No stats collection after 30 seconds.
      with test_lib.FakeTime(now + 30):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEmpty(runs)

      # Let 61 seconds pass.
      with test_lib.FakeTime(now + 61):
        self.client_communicator.client_worker.stats_collector._Send()
        # This time the client should collect stats.
        self.assertLen(runs, 1)

      # No stats collection within one minute from the last time.
      with test_lib.FakeTime(now + 61 + 59):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertLen(runs, 1)

      # Stats collection happens as more than one minute has passed since the
      # last one.
      with test_lib.FakeTime(now + 61 + 61):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertLen(runs, 2)

  def testClientStatsCollectionAlwaysHappensAfterHandleMessage(self):
    """Tests that client stats are collected more often when client is busy."""
    now = 1000000
    # Pretend we have already sent stats.
    self.client_communicator.client_worker.stats_collector._last_send_time = (
        rdfvalue.RDFDatetime.FromSecondsSinceEpoch(now))

    with test_lib.FakeTime(now):
      self.client_communicator.client_worker.stats_collector._Send()

    runs = []
    with mock.patch.object(admin.GetClientStatsAuto, "Run",
                           lambda cls, _: runs.append(1)):

      # No stats collection after 30 seconds.
      with test_lib.FakeTime(now + 30):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEmpty(runs)

      msg = rdf_flows.GrrMessage(
          name=standard.HashFile.__name__, generate_task_id=True)
      self.client_communicator.client_worker.HandleMessage(msg)

      # HandleMessage was called, but one minute hasn't passed, so
      # stats should not be sent.
      with test_lib.FakeTime(now + 59):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEmpty(runs)

      # HandleMessage was called more than one minute ago, so stats
      # should be sent.
      with test_lib.FakeTime(now + 61):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertLen(runs, 1)

  def RaiseError(self, **_):
    raise MakeHTTPException(500, "Not a real connection.")

  def testClientConnectionErrors(self):
    client_obj = comms.GRRHTTPClient(worker_cls=worker_mocks.ClientWorker)
    # Make the connection unavailable and skip the retry interval.
    with utils.MultiStubber(
        (requests, "request", self.RaiseError),
        (client_obj.http_manager, "connection_error_limit", 8)):
      # Simulate a client run. The client will retry the connection limit by
      # itself. The Run() method will quit when connection_error_limit is
      # reached. This will make the real client quit.
      client_obj.Run()

      self.assertEqual(client_obj.http_manager.consecutive_connection_errors, 9)


class FrontEndServerTest(absltest.TestCase):

  def setUp(self):
    super().setUp()

    private_key = rsa.generate_private_key(65537, 2048)
    certificate = x509.CertificateBuilder(
        subject_name=x509.Name([
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "GRR Frontend"),
        ]),
        issuer_name=x509.Name([
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "GRR"),
        ]),
        serial_number=x509.random_serial_number(),
        not_valid_before=datetime.datetime.now() - datetime.timedelta(days=1),
        not_valid_after=datetime.datetime.now() + datetime.timedelta(days=1),
        public_key=private_key.public_key(),
    ).sign(private_key, hashes.SHA256())

    rdf_private_key = rdf_crypto.RSAPrivateKey(private_key)
    rdf_certificate = rdf_crypto.RDFX509Cert(certificate)

    self.server = frontend_lib.FrontEndServer(
        private_key=rdf_private_key,
        certificate=rdf_certificate,
    )

  @db_test_lib.WithDatabase
  def testReceiveRRGResponseStatusOK(self, db: abstract_db.Database):
    client_id = db_test_utils.InitializeClient(db)
    flow_id = db_test_utils.InitializeFlow(db, client_id)

    flow_request = rdf_flow_objects.FlowRequest()
    flow_request.client_id = client_id
    flow_request.flow_id = flow_id
    flow_request.request_id = 1337
    db.WriteFlowRequests([flow_request])

    response = rrg_pb2.Response()
    response.flow_id = int(flow_id, 16)
    response.request_id = 1337
    response.response_id = 42
    response.status.network_bytes_sent = 4 * 1024 * 1024

    self.server.ReceiveRRGResponse(client_id, response)

    flow_responses = db.ReadAllFlowRequestsAndResponses(client_id, flow_id)
    self.assertLen(flow_responses, 1)

    flow_response = flow_responses[0][1][response.response_id]
    self.assertIsInstance(flow_response, rdf_flow_objects.FlowStatus)
    self.assertEqual(flow_response.client_id, client_id)
    self.assertEqual(flow_response.flow_id, flow_id)
    self.assertEqual(flow_response.request_id, 1337)
    self.assertEqual(flow_response.response_id, 42)

    self.assertEqual(
        flow_response.status,
        rdf_flow_objects.FlowStatus.Status.OK,
    )
    self.assertEqual(flow_response.network_bytes_sent, 4 * 1024 * 1024)

  @db_test_lib.WithDatabase
  def testReceiveRRGResponseStatusError(self, db: abstract_db.Database):
    client_id = db_test_utils.InitializeClient(db)
    flow_id = db_test_utils.InitializeFlow(db, client_id)

    flow_request = rdf_flow_objects.FlowRequest()
    flow_request.client_id = client_id
    flow_request.flow_id = flow_id
    flow_request.request_id = 1337
    db.WriteFlowRequests([flow_request])

    response = rrg_pb2.Response()
    response.flow_id = int(flow_id, 16)
    response.request_id = 1337
    response.response_id = 42
    response.status.error.type = rrg_pb2.Status.Error.UNSUPPORTED_ACTION
    response.status.error.message = "foobar"

    self.server.ReceiveRRGResponse(client_id, response)

    flow_responses = db.ReadAllFlowRequestsAndResponses(client_id, flow_id)
    self.assertLen(flow_responses, 1)

    flow_response = flow_responses[0][1][response.response_id]
    self.assertIsInstance(flow_response, rdf_flow_objects.FlowStatus)
    self.assertEqual(flow_response.client_id, client_id)
    self.assertEqual(flow_response.flow_id, flow_id)
    self.assertEqual(flow_response.request_id, 1337)
    self.assertEqual(flow_response.response_id, 42)

    self.assertEqual(
        flow_response.status,
        rdf_flow_objects.FlowStatus.Status.ERROR,
    )
    self.assertEqual(flow_response.error_message, "foobar")

  @db_test_lib.WithDatabase
  def testReceiveRRGResponseResult(self, db: abstract_db.Database):
    client_id = db_test_utils.InitializeClient(db)
    flow_id = db_test_utils.InitializeFlow(db, client_id)

    flow_request = rdf_flow_objects.FlowRequest()
    flow_request.client_id = client_id
    flow_request.flow_id = flow_id
    flow_request.request_id = 1337
    db.WriteFlowRequests([flow_request])

    response = rrg_pb2.Response()
    response.flow_id = int(flow_id, 16)
    response.request_id = 1337
    response.response_id = 42
    response.result.Pack(wrappers_pb2.StringValue(value="foobar"))

    self.server.ReceiveRRGResponse(client_id, response)

    flow_responses = db.ReadAllFlowRequestsAndResponses(client_id, flow_id)
    self.assertLen(flow_responses, 1)

    flow_response = flow_responses[0][1][response.response_id]
    self.assertIsInstance(flow_response, rdf_flow_objects.FlowResponse)
    self.assertEqual(flow_response.client_id, client_id)
    self.assertEqual(flow_response.flow_id, flow_id)
    self.assertEqual(flow_response.request_id, 1337)
    self.assertEqual(flow_response.response_id, 42)

    string = wrappers_pb2.StringValue()
    string.ParseFromString(flow_response.any_payload.value)
    self.assertEqual(string.value, "foobar")

  @db_test_lib.WithDatabase
  def testReceiveRRGResponseUnexpected(self, db: abstract_db.Database):
    client_id = db_test_utils.InitializeClient(db)
    flow_id = db_test_utils.InitializeFlow(db, client_id)

    flow_request = rdf_flow_objects.FlowRequest()
    flow_request.client_id = client_id
    flow_request.flow_id = flow_id
    flow_request.request_id = 1337
    db.WriteFlowRequests([flow_request])

    response = rrg_pb2.Response()
    response.flow_id = int(flow_id, 16)
    response.request_id = 1337
    response.response_id = 42

    with self.assertRaisesRegex(ValueError, "Unexpected response"):
      self.server.ReceiveRRGResponse(client_id, response)

  def testReceiveRRGParcelOK(self):
    client_id = "C.1234567890ABCDEF"

    sink = sinks_test_lib.FakeSink()

    with mock.patch.object(sinks, "REGISTRY", {rrg_pb2.STARTUP: sink}):
      parcel = rrg_pb2.Parcel()
      parcel.sink = rrg_pb2.STARTUP
      parcel.payload.value = b"FOO"
      self.server.ReceiveRRGParcel(client_id, parcel)

      parcel = rrg_pb2.Parcel()
      parcel.sink = rrg_pb2.STARTUP
      parcel.payload.value = b"BAR"
      self.server.ReceiveRRGParcel(client_id, parcel)

    parcels = sink.Parcels(client_id)
    self.assertLen(parcels, 2)
    self.assertEqual(parcels[0].payload.value, b"FOO")
    self.assertEqual(parcels[1].payload.value, b"BAR")

  def testReceiveRRGParcelError(self):
    class FakeSink(sinks.Sink):

      # TODO: Add the `@override` annotation [1] once we are can
      # use Python 3.12 features.
      #
      # [1]: https://peps.python.org/pep-0698/
      def Accept(self, client_id: str, parcel: rrg_pb2.Parcel) -> None:
        del client_id, parcel  # Unused.
        raise RuntimeError()

    with mock.patch.object(sinks, "REGISTRY", {rrg_pb2.STARTUP: FakeSink()}):
      parcel = rrg_pb2.Parcel()
      parcel.sink = rrg_pb2.STARTUP
      parcel.payload.value = b"FOO"
      # Should not raise.
      self.server.ReceiveRRGParcel("C.1234567890ABCDEF", parcel)


def main(args):
  test_lib.main(args)


if __name__ == "__main__":
  app.run(main)
