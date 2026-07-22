/** Community requests have no commercial entitlement error contract. */
export function createEditionAccessError(
  _status: number,
  _payload: unknown,
  _readMessage: (payload: unknown, fallback: string) => string,
): null {
  return null;
}

export function isEditionAccessError(_error: unknown): _error is Error {
  return false;
}
