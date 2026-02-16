"""Tests for retry_with_backoff and custom exceptions."""

import time
import pytest
from unittest.mock import MagicMock, patch

# Allow import from parent dir
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from label_analyzer_production import (
    retry_with_backoff,
    APIError,
    LabelAnalyzerError,
    CalibrationError,
    DetectionError,
)


class TestRetryWithBackoff:
    """Tests for the retry_with_backoff utility."""

    def test_succeeds_first_try(self):
        fn = MagicMock(return_value="ok")
        result = retry_with_backoff(fn, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert fn.call_count == 1

    def test_succeeds_after_transient_failure(self):
        fn = MagicMock(side_effect=[ConnectionError("down"), "ok"])
        result = retry_with_backoff(
            fn, max_retries=3, base_delay=0.01,
            retryable_exceptions=(ConnectionError,),
        )
        assert result == "ok"
        assert fn.call_count == 2

    def test_exhausts_retries_raises_api_error(self):
        fn = MagicMock(side_effect=TimeoutError("slow"))
        with pytest.raises(APIError) as exc_info:
            retry_with_backoff(
                fn, max_retries=2, base_delay=0.01,
                retryable_exceptions=(TimeoutError,),
            )
        assert exc_info.value.attempts == 3  # 1 initial + 2 retries
        assert isinstance(exc_info.value.last_exception, TimeoutError)

    def test_non_retryable_exception_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError):
            retry_with_backoff(
                fn, max_retries=5, base_delay=0.01,
                retryable_exceptions=(ConnectionError,),
            )
        assert fn.call_count == 1

    def test_zero_retries_means_single_attempt(self):
        fn = MagicMock(side_effect=ConnectionError("fail"))
        with pytest.raises(APIError) as exc_info:
            retry_with_backoff(
                fn, max_retries=0, base_delay=0.01,
                retryable_exceptions=(ConnectionError,),
            )
        assert exc_info.value.attempts == 1

    def test_backoff_delay_increases(self):
        """Verify that delays increase roughly exponentially."""
        fn = MagicMock(side_effect=[OSError, OSError, "ok"])
        t0 = time.monotonic()
        retry_with_backoff(
            fn, max_retries=3, base_delay=0.05, jitter=False,
            retryable_exceptions=(OSError,),
        )
        elapsed = time.monotonic() - t0
        # base_delay=0.05 → delays of 0.05 + 0.10 = 0.15s minimum
        assert elapsed >= 0.12  # allow some slack


class TestExceptionHierarchy:
    """Verify custom exceptions inherit correctly."""

    def test_api_error_is_label_analyzer_error(self):
        assert issubclass(APIError, LabelAnalyzerError)

    def test_calibration_error_is_label_analyzer_error(self):
        assert issubclass(CalibrationError, LabelAnalyzerError)

    def test_detection_error_is_label_analyzer_error(self):
        assert issubclass(DetectionError, LabelAnalyzerError)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
