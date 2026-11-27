from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import MagicMock, Mock, patch

from ofac_checker import (
    NAME_INPUT,
    RESULT_LABEL,
    SEARCH_BUTTON,
    OfacResult,
    OfacUnavailable,
    SearchPacing,
    search_company,
    search_ofac_names,
    validate_response,
    write_pdf,
)


class BatchTests(TestCase):
    def setUp(self):
        self.clock = 0.0
        self.cancel = Event()
        self.pacing = SearchPacing()
        playwright_patch = patch("ofac_checker.sync_playwright")
        playwright = playwright_patch.start().return_value.__enter__.return_value
        self.addCleanup(playwright_patch.stop)
        browser = playwright.chromium.launch.return_value
        browser.is_connected.return_value = True
        browser.new_page.return_value.is_closed.return_value = False
        patches = (
            patch("ofac_checker.monotonic", side_effect=lambda: self.clock),
            patch.object(self.cancel, "wait", side_effect=self.wait),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait(self, delay):
        self.clock += delay
        return self.cancel.is_set()

    def test_batch_recovers_from_rate_limiting_after_100(self):
        names = [f"Test name {index}" for index in range(1000)]
        submissions = []

        def submit(_page, name):
            submissions.append((name, self.clock))
            if len(submissions) == 101:
                validate_response(SimpleNamespace(ok=False, status=429, headers={"retry-after": "37"}))
            return OfacResult(name, "Clean", "Lookup Results: 0 Found")

        with patch("ofac_checker.open_ofac") as open_form, patch("ofac_checker.search_company", side_effect=submit):
            results = search_ofac_names(names, cancel_event=self.cancel, pacing=self.pacing)

        self.assertEqual([result.company for result in results], names)
        self.assertEqual(len(submissions), 1001)
        self.assertEqual(open_form.call_count, 2)
        self.assertEqual(submissions[100][0], submissions[101][0])
        self.assertGreaterEqual(submissions[101][1] - submissions[100][1], 37)

    def test_cancellation_during_retry_keeps_progress_and_cooldown(self):
        checked = OfacResult("First name", "Clean", "Lookup Results: 0 Found")
        failure = OfacUnavailable("OFAC returned HTTP 429", retryable=True, retry_after=55, rate_limited=True)

        def progress(message):
            if "Waiting to retry" in message:
                self.cancel.set()

        with patch("ofac_checker.open_ofac"), patch("ofac_checker.search_company", side_effect=[checked, failure]):
            results = search_ofac_names(
                ["First name", "Second name"], progress=progress, cancel_event=self.cancel, pacing=self.pacing
            )

        self.assertEqual(results, [checked])
        self.assertEqual(self.pacing.seconds_remaining(), 55)

    def test_access_denied_pauses_after_completed_results(self):
        completed = []
        checked = OfacResult("First name", "Clean", "Lookup Results: 0 Found")

        def submit(_page, name):
            if name == checked.company:
                return checked
            validate_response(SimpleNamespace(ok=False, status=403, headers={}))

        with (
            patch("ofac_checker.open_ofac"),
            patch("ofac_checker.search_company", side_effect=submit) as search,
            self.assertRaisesRegex(OfacUnavailable, "HTTP 403"),
        ):
            search_ofac_names(
                ["First name", "Second name", "Third name"],
                result_callback=completed.append,
                cancel_event=self.cancel,
                pacing=self.pacing,
            )

        self.assertEqual(completed, [checked])
        self.assertEqual(search.call_count, 2)

    def test_resume_honors_a_long_requested_cooldown(self):
        failure = OfacUnavailable("OFAC returned HTTP 429", retryable=True, retry_after=600, rate_limited=True)
        with patch("ofac_checker.open_ofac") as open_form, patch("ofac_checker.search_company", side_effect=failure):
            with self.assertRaisesRegex(OfacUnavailable, "resuming in 600 seconds"):
                search_ofac_names(["Test name"], cancel_event=self.cancel, pacing=self.pacing)
            with self.assertRaisesRegex(OfacUnavailable, "cooldown"):
                search_ofac_names(["Test name"], cancel_event=self.cancel, pacing=self.pacing)
        self.assertEqual(open_form.call_count, 1)


class PrintoutTests(TestCase):
    def test_site_results_must_match_the_query(self):
        page = MagicMock()
        name_input = Mock()
        label = Mock()
        page.locator.side_effect = {NAME_INPUT: name_input, RESULT_LABEL: label, SEARCH_BUTTON: Mock()}.__getitem__
        page.expect_response.return_value.__enter__.return_value.value.ok = True
        label.inner_text.return_value = "Lookup Results: 10 Found"
        name_input.input_value.return_value = "Example"
        self.assertEqual(search_company(page, "Example").status, "Review needed")

        name_input.input_value.side_effect = ["Example", "Another name"]
        with self.assertRaisesRegex(OfacUnavailable, "verified result"):
            search_company(page, "Example")

    def test_pdf_prints_the_site_page_in_the_original_format(self):
        page = Mock()
        page.pdf.return_value = b"%PDF-test"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "proof.pdf"
            write_pdf(page, path)
            self.assertEqual(path.read_bytes(), b"%PDF-test")

        page.set_content.assert_not_called()
        page.pdf.assert_called_once_with(
            landscape=True,
            format="Legal",
            scale=0.84,
            print_background=False,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )


if __name__ == "__main__":
    main()
