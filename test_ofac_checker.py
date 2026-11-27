from io import BytesIO
from threading import Event
from unittest import TestCase, main
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ofac_checker import search_ofac_names


def sanctions_list(name: str, count: int = 1) -> BytesIO:
    data = f"""<sdnList xmlns="urn:ofac-test">
    <publshInformation>
        <Publish_Date>10/01/2026</Publish_Date><Record_Count>{count}</Record_Count>
    </publshInformation>
    <sdnEntry>
        <uid>123</uid><lastName>{name}</lastName><sdnType>Entity</sdnType>
        <akaList>
            <aka><category>strong</category><firstName>Jose&#769;</firstName><lastName>Sample</lastName></aka>
        </akaList>
    </sdnEntry>
</sdnList>""".encode()
    response = BytesIO(data)
    response.headers = {"Content-Length": str(len(data))}
    return response


class ScreeningTests(TestCase):
    def test_large_batch_retries_rate_limit_and_checks_both_lists_locally(self):
        cancel = Event()
        cancel.wait = Mock(return_value=False)
        throttle = HTTPError("https://example.test", 429, "Too Many Requests", {"Retry-After": "17"}, BytesIO())
        names = ["SDN, Company", "Non-SDN Company", "José Sample", "Sample, José"]
        names += [f"Test name {index}" for index in range(996)]

        responses = [throttle, sanctions_list("SDN Company"), sanctions_list("Non-SDN Company")]
        with patch("sanctions_data.urlopen", side_effect=responses) as download:
            results = search_ofac_names(names, cancel_event=cancel)

        cancel.wait.assert_called_once_with(17)
        self.assertEqual(download.call_count, 3)
        self.assertEqual([result.company for result in results], names)
        self.assertEqual([result.status for result in results[:4]], ["Review needed"] * 4)
        self.assertEqual(results[2].result_text, "2 potential matches")
        self.assertTrue(all(result.display_status == "No exact match" for result in results[4:]))

    def test_cancel_keeps_completed_results(self):
        cancel = Event()
        received = []

        def on_result(result):
            received.append(result)
            if len(received) == 3:
                cancel.set()

        with patch("sanctions_data.urlopen", side_effect=[sanctions_list("SDN"), sanctions_list("Non-SDN")]):
            results = search_ofac_names(
                [f"Test name {index}" for index in range(1000)], result_callback=on_result, cancel_event=cancel
            )

        self.assertEqual(len(results), 3)
        self.assertEqual(results, received)

    def test_cancel_during_retry_wait_stops_requests(self):
        cancel = Event()
        throttle = HTTPError("https://example.test", 429, "Too Many Requests", {"Retry-After": "60"}, BytesIO())

        def progress(message):
            if "Retrying" in message:
                cancel.set()

        with patch("sanctions_data.urlopen", side_effect=throttle) as download:
            self.assertEqual(search_ofac_names(["Test name"], progress=progress, cancel_event=cancel), [])
        download.assert_called_once()

    def test_incomplete_second_list_cannot_publish_results(self):
        on_result = Mock()
        with patch("sanctions_data.urlopen", side_effect=[sanctions_list("SDN"), sanctions_list("Non-SDN", count=2)]):
            with self.assertRaisesRegex(RuntimeError, "Consolidated Non-SDN list could not be verified"):
                search_ofac_names(["Test name"], result_callback=on_result)
        on_result.assert_not_called()


if __name__ == "__main__":
    main()
