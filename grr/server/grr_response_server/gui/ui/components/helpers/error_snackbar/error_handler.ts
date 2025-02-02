import {HttpErrorResponse} from '@angular/common/http';
import {ErrorHandler, Injectable} from '@angular/core';
import {MatLegacySnackBar} from '@angular/material/legacy-snack-bar';

import {ErrorSnackBar} from './error_snackbar';

/** Error handler that shows the error message in a SnackBar. */
@Injectable()
export class SnackBarErrorHandler extends ErrorHandler {
  constructor(private readonly snackBar: MatLegacySnackBar) {
    super();
  }

  override handleError(error: unknown) {
    console.error(error);

    if (error instanceof Error) {
      error = error.message;
    }

    // HttpApiService already shows Snackbars for HTTP error responses.
    if (!(error instanceof HttpErrorResponse)) {
      this.snackBar.openFromComponent(ErrorSnackBar, {data: String(error)});
    }
  }
}
