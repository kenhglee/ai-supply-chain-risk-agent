"""One fenced TransactWriteItems with the retry rules of the ownership design.

docs/SAME_THREAD_OWNERSHIP_DESIGN.md §5. Used by the fenced checkpoint saver,
the finalization store and the action ledger. Every transaction contains a
ConditionCheck on the thread's lease item; the caller says where it is.

Classification, in order:
  lost ownership       the lease check's cancellation reason is
                       ConditionalCheckFailed -> LeaseLost, never retried
  data conflict        another item's condition failed -> verify() decides
                       (applied: success; anything else: raise)
  transaction conflict TransactionConflict / throttling -> retried with the
                       same request and ClientRequestToken
  unknown outcome      timeouts, connection errors, 5xx -> retried, then
                       verify(); still not applied -> LeaseLost if no longer
                       the owner, else WriteOutcomeUnknown
  request errors       anything else (validation, token mismatch) -> raised
"""
import random
import time

from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, HTTPClientError

from app.storage.thread_lease import LeaseLost, WriteConflict, WriteOutcomeUnknown

APPLIED, ABSENT = "applied", "absent"
RETRYABLE_ERROR_CODES = frozenset({
    "ProvisionedThroughputExceededException", "ThrottlingException", "RequestLimitExceeded",
    "TransactionInProgressException", "InternalServerError", "ServiceUnavailable",
})
RETRYABLE_CANCELLATION_REASONS = frozenset({"TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded"})
UNKNOWN_OUTCOME_EXCEPTIONS = (HTTPClientError, BotoConnectionError)


def run_fenced_transaction(client, *, token: tuple[str, str, int], operation: str, items: list[dict],
                           request_token: str | None, verify, still_owner, lease_index: int = 0,
                           max_attempts: int = 5, backoff_s: float = 0.05, sleep=time.sleep) -> None:
    """Apply `items` atomically or raise; see module doc for the classification."""
    unknown = False
    kwargs = {"TransactItems": items}
    if request_token:
        kwargs["ClientRequestToken"] = request_token
    for attempt in range(max_attempts):
        try:
            client.transact_write_items(**kwargs)
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "TransactionCanceledException":
                reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                if len(reasons) > lease_index and reasons[lease_index] == "ConditionalCheckFailed":
                    raise LeaseLost(token[0], token[1], token[2], operation) from None
                others = [r for i, r in enumerate(reasons) if i != lease_index]
                if "ConditionalCheckFailed" in others:
                    if verify() == APPLIED:
                        return
                    raise WriteConflict(f"{operation}: a conflicting item exists") from None
                if not set(reasons) & RETRYABLE_CANCELLATION_REASONS:
                    raise
            elif code not in RETRYABLE_ERROR_CODES:
                raise  # IdempotentParameterMismatch, ValidationException, AccessDenied, ...
            unknown = unknown or code in {"InternalServerError", "ServiceUnavailable"}
        except UNKNOWN_OUTCOME_EXCEPTIONS:
            unknown = True
        sleep(backoff_s * (2 ** attempt) * (1 + random.random()))
    if verify() == APPLIED:
        return
    if not still_owner():
        raise LeaseLost(token[0], token[1], token[2], operation)
    raise WriteOutcomeUnknown(f"{operation}: not applied after {max_attempts} attempts"
                              + (" (unknown outcome)" if unknown else ""))
