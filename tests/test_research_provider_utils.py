import unittest

from scripts.research_provider_utils import call_with_retry


class ProviderRetryTests(unittest.TestCase):
    def test_retries_transient_failure_then_returns_value(self):
        calls = []
        sleeps = []

        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("temporary timeout")
            return {"rows": 3}

        result = call_with_retry(operation, operation="fixture", attempts=2,
                                 delay_seconds=0.5, sleep=sleeps.append)
        self.assertEqual(result, {"rows": 3})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.5])

    def test_exhaustion_reports_attempts_and_last_failure(self):
        def operation():
            raise ConnectionError("provider unavailable")

        with self.assertRaisesRegex(RuntimeError, "2 bounded attempt"):
            call_with_retry(operation, operation="fixture", attempts=2,
                             delay_seconds=0, sleep=lambda _: None)

    def test_non_retryable_provider_error_stops_without_sleep(self):
        calls = []
        sleeps = []

        def operation():
            calls.append(1)
            raise RuntimeError("rate limited")

        with self.assertRaisesRegex(RuntimeError, "1 bounded attempt"):
            call_with_retry(operation, operation="fixture", attempts=3,
                             delay_seconds=1, sleep=sleeps.append,
                             should_retry=lambda exc: "rate limited" not in str(exc))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_invalid_retry_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            call_with_retry(lambda: None, operation="fixture", attempts=0)
        with self.assertRaises(ValueError):
            call_with_retry(lambda: None, operation="fixture", delay_seconds=-1)


if __name__ == "__main__":
    unittest.main()
