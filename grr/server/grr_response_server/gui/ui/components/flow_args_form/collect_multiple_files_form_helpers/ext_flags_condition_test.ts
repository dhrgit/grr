import {TestbedHarnessEnvironment} from '@angular/cdk/testing/testbed';
import {TestBed, waitForAsync} from '@angular/core/testing';
import {ControlContainer} from '@angular/forms';
import {MatLegacyButtonHarness} from '@angular/material/legacy-button/testing';
import {NoopAnimationsModule} from '@angular/platform-browser/animations';

import {FileFinderExtFlagsCondition} from '../../../lib/api/api_interfaces';
import {newClient} from '../../../lib/models/model_test_util';
import {getLinuxFlagMaskByNames, getOsxFlagMaskByNames} from '../../../lib/models/os_extended_flags';
import {ClientPageGlobalStore} from '../../../store/client_page_global_store';
import {ClientPageGlobalStoreMock, mockClientPageGlobalStore} from '../../../store/client_page_global_store_test_util';
import {initTestEnvironment} from '../../../testing';

import {ExtFlagsCondition, MaskCondition, updateFlagConditionsForOS} from './ext_flags_condition';
import {HelpersModule} from './module';

initTestEnvironment();

describe('ExtFlagsCondition component', () => {
  let control: ReturnType<typeof ExtFlagsCondition['createFormGroup']>;
  let clientPageGlobalStore: ClientPageGlobalStoreMock;

  beforeEach(waitForAsync(() => {
    control = ExtFlagsCondition.createFormGroup();
    clientPageGlobalStore = mockClientPageGlobalStore();

    TestBed
        .configureTestingModule({
          imports: [
            NoopAnimationsModule,
            HelpersModule,
          ],
          providers: [
            {
              provide: ControlContainer,
              useFactory: () => ({control}),
            },
            {
              provide: ClientPageGlobalStore,
              useFactory: () => clientPageGlobalStore,
            },
          ],
          teardown: {destroyAfterEach: false}
        })
        .compileComponents();
  }));

  it('sets bits when clicking once on flag', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    const loader = TestbedHarnessEnvironment.loader(fixture);
    fixture.detectChanges();

    const button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /nodump/}));
    await button.click();

    const expected: FileFinderExtFlagsCondition = {
      linuxBitsSet: 0,
      linuxBitsUnset: 0,
      osxBitsSet: getOsxFlagMaskByNames(['UF_NODUMP']),
      osxBitsUnset: 0,
    };

    expect(control.value).toEqual(expected);
  });

  it('unsets bits when clicking twice on flag', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    const loader = TestbedHarnessEnvironment.loader(fixture);
    fixture.detectChanges();

    const button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /nodump/}));
    await button.click();
    await button.click();

    const expected: FileFinderExtFlagsCondition = {
      linuxBitsSet: 0,
      linuxBitsUnset: 0,
      osxBitsSet: 0,
      osxBitsUnset: getOsxFlagMaskByNames(['UF_NODUMP']),
    };

    expect(control.value).toEqual(expected);
  });

  it('ignores bits when clicking three times on flag', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    const loader = TestbedHarnessEnvironment.loader(fixture);
    fixture.detectChanges();

    const button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /nodump/}));
    await button.click();
    await button.click();
    await button.click();

    const expected: FileFinderExtFlagsCondition = {
      linuxBitsSet: 0,
      linuxBitsUnset: 0,
      osxBitsSet: 0,
      osxBitsUnset: 0,
    };

    expect(control.value).toEqual(expected);
  });

  it('computes logical OR of all checked flags', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    const loader = TestbedHarnessEnvironment.loader(fixture);
    fixture.detectChanges();

    let button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /nodump/}));
    await button.click();

    button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /opaque/}));
    await button.click();

    const expected: FileFinderExtFlagsCondition = {
      linuxBitsSet: 0,
      linuxBitsUnset: 0,
      osxBitsSet: getOsxFlagMaskByNames(['UF_NODUMP', 'UF_OPAQUE']),
      osxBitsUnset: 0,
    };

    expect(control.value).toEqual(expected);
  });

  it('can require set and unset flags', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    const loader = TestbedHarnessEnvironment.loader(fixture);
    fixture.detectChanges();

    let button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /nodump/}));
    await button.click();

    button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /opaque/}));
    await button.click();
    await button.click();

    const expected: FileFinderExtFlagsCondition = {
      linuxBitsSet: 0,
      linuxBitsUnset: 0,
      osxBitsSet: getOsxFlagMaskByNames(['UF_NODUMP']),
      osxBitsUnset: getOsxFlagMaskByNames(['UF_OPAQUE']),
    };

    expect(control.value).toEqual(expected);
  });

  it('can set linux and osx flags', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    const loader = TestbedHarnessEnvironment.loader(fixture);
    fixture.detectChanges();

    let button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /T/}));
    await button.click();

    button =
        await loader.getHarness(MatLegacyButtonHarness.with({text: /nodump/}));
    await button.click();
    await button.click();

    const expected: FileFinderExtFlagsCondition = {
      linuxBitsSet: getLinuxFlagMaskByNames(['FS_TOPDIR_FL']),
      linuxBitsUnset: 0,
      osxBitsSet: 0,
      osxBitsUnset: getOsxFlagMaskByNames(['UF_NODUMP']),
    };

    expect(control.value).toEqual(expected);
  });

  it('shows only macOS flags (not linux) if client is macOS', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    clientPageGlobalStore.mockedObservables.selectedClient$.next(
        newClient({knowledgeBase: {os: 'Darwin'}}));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('macOS');
    expect(fixture.nativeElement.textContent).toContain('nodump');
    expect(fixture.nativeElement.textContent).not.toContain('Linux');
    expect(fixture.nativeElement.textContent).not.toContain('T');
  });

  it('shows only linux flags (not macOS) if client is linux', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    clientPageGlobalStore.mockedObservables.selectedClient$.next(
        newClient({knowledgeBase: {os: 'Linux'}}));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('macOS');
    expect(fixture.nativeElement.textContent).not.toContain('nodump');
    expect(fixture.nativeElement.textContent).toContain('Linux');
    expect(fixture.nativeElement.textContent).toContain('T');
  });

  it('shows both linux and macOS flags for other clients', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    clientPageGlobalStore.mockedObservables.selectedClient$.next(
        newClient({knowledgeBase: {os: 'Windows'}}));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('macOS');
    expect(fixture.nativeElement.textContent).toContain('nodump');
    expect(fixture.nativeElement.textContent).toContain('Linux');
    expect(fixture.nativeElement.textContent).toContain('T');
  });

  it('shows both linux and macOS flags for unknown clients', async () => {
    const fixture = TestBed.createComponent(ExtFlagsCondition);
    clientPageGlobalStore.mockedObservables.selectedClient$.next(
        newClient({knowledgeBase: {os: undefined}}));
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('macOS');
    expect(fixture.nativeElement.textContent).toContain('nodump');
    expect(fixture.nativeElement.textContent).toContain('Linux');
    expect(fixture.nativeElement.textContent).toContain('T');
  });
});

describe('updateFlagConditionsForOS', () => {
  it('should set the condition of elements with a bit mask of 4 to 1', () => {
    const testFlags = [
      {
        name: 'test',
        identifier: 'test',
        mask: 4,  // 100
        description: 'test',
        condition: 0,
      },
    ];

    updateFlagConditionsForOS(testFlags, 4, MaskCondition.REQUIRE_SET);

    expect(testFlags[0].condition).toEqual(MaskCondition.REQUIRE_SET);
  });

  it('should set the condition of elements with a bit mask of 2 to 2', () => {
    const testFlags = [
      {
        name: 'test',
        identifier: 'test',
        mask: 2,  // 10
        description: 'test',
        condition: 0,
      },
    ];

    updateFlagConditionsForOS(testFlags, 2, MaskCondition.REQUIRE_UNSET);

    expect(testFlags[0].condition).toEqual(MaskCondition.REQUIRE_UNSET);
  });

  it('should set the condition of elements with a bit mask of 1 to 3', () => {
    const testFlags = [
      {
        name: 'test',
        identifier: 'test',
        mask: 17,  // 10001
        description: 'test',
        condition: 0,
      },
      {
        name: 'test',
        identifier: 'test',
        mask: 1,  // 1
        description: 'test',
        condition: 0,
      },
    ];

    updateFlagConditionsForOS(testFlags, 1, MaskCondition.REQUIRE_SET);

    expect(testFlags[0].condition).toEqual(MaskCondition.REQUIRE_SET);
    expect(testFlags[1].condition).toEqual(MaskCondition.REQUIRE_SET);
  });

  it('should set the condition of elements with a bit mask of 32 to 5 (there are none)',
     () => {
       const testFlags = [
         {
           name: 'test',
           identifier: 'test',
           mask: 2,  // 10
           description: 'test',
           condition: 0,
         },
         {
           name: 'test',
           identifier: 'test',
           mask: 17,  // 10001
           description: 'test',
           condition: 0,
         },
         {
           name: 'test',
           identifier: 'test',
           mask: 4,  // 100
           description: 'test',
           condition: 0,
         },
         {
           name: 'test',
           identifier: 'test',
           mask: 1,  // 1
           description: 'test',
           condition: 0,
         },
       ];

       updateFlagConditionsForOS(testFlags, 32, MaskCondition.REQUIRE_SET);

       expect(testFlags.find(f => f.condition === MaskCondition.REQUIRE_SET))
           .toBeUndefined();
     });
});
