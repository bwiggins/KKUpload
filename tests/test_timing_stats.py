from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from timing_stats import (
    estimate_upload_time,
    new_upload_timing_sample,
    record_upload_timing,
)


class TimingStatsTests(unittest.TestCase):
    def test_estimate_uses_previous_upload_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = Path(temp_dir) / "timing_stats.json"
            record_upload_timing(
                stats_path,
                new_upload_timing_sample(
                    file_count=10,
                    source_bytes=1000,
                    uploaded_bytes=1000,
                    upload_seconds=10,
                    total_seconds=20,
                ),
            )

            estimate = estimate_upload_time(
                stats_path,
                file_count=5,
                source_bytes=500,
            )

            self.assertEqual(estimate.sample_count, 1)
            self.assertAlmostEqual(estimate.upload_bytes_per_second, 100)
            self.assertAlmostEqual(estimate.overhead_seconds_per_file, 1)
            self.assertAlmostEqual(estimate.seconds, 10)

    def test_missing_stats_returns_unknown_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            estimate = estimate_upload_time(
                Path(temp_dir) / "missing.json",
                file_count=5,
                source_bytes=500,
            )

            self.assertIsNone(estimate.seconds)
            self.assertEqual(estimate.sample_count, 0)
            self.assertFalse(estimate.used_fallback)

    def test_estimate_uses_latest_previous_upload_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = Path(temp_dir) / "timing_stats.json"
            stats_path.write_text(
                """
                {
                  "samples": [
                    {
                      "completed_at": "2026-01-01T00:00:00+00:00",
                      "file_count": 1,
                      "source_bytes": 1000,
                      "uploaded_bytes": 1000,
                      "upload_seconds": 100,
                      "total_seconds": 101
                    },
                    {
                      "completed_at": "2026-01-02T00:00:00+00:00",
                      "file_count": 10,
                      "source_bytes": 1000,
                      "uploaded_bytes": 1000,
                      "upload_seconds": 10,
                      "total_seconds": 20
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )

            estimate = estimate_upload_time(
                stats_path,
                file_count=5,
                source_bytes=500,
            )

            self.assertEqual(estimate.sample_count, 1)
            self.assertAlmostEqual(estimate.upload_bytes_per_second, 100)
            self.assertAlmostEqual(estimate.overhead_seconds_per_file, 1)
            self.assertAlmostEqual(estimate.seconds, 10)

    def test_record_upload_timing_replaces_previous_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stats_path = Path(temp_dir) / "timing_stats.json"
            record_upload_timing(
                stats_path,
                new_upload_timing_sample(
                    file_count=1,
                    source_bytes=1000,
                    uploaded_bytes=1000,
                    upload_seconds=100,
                    total_seconds=101,
                ),
            )
            record_upload_timing(
                stats_path,
                new_upload_timing_sample(
                    file_count=10,
                    source_bytes=1000,
                    uploaded_bytes=1000,
                    upload_seconds=10,
                    total_seconds=20,
                ),
            )

            estimate = estimate_upload_time(
                stats_path,
                file_count=5,
                source_bytes=500,
            )

            self.assertEqual(estimate.sample_count, 1)
            self.assertAlmostEqual(estimate.seconds, 10)


if __name__ == "__main__":
    unittest.main()
