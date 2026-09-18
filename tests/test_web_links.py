"""Links the web layer builds.

A grouping key can contain anything a file name can, and Jinja escapes it for
HTML only. On a plain order number none of this shows, which is why it is
tested. The back link of the detail page comes out of the referer, which the
visitor sets.
"""

import unittest

from starlette.requests import Request

from pressledger import config
from pressledger.web.app import _back_to_jobs, _path_segment


class PathSegmentTests(unittest.TestCase):
    def test_the_four_characters_that_break_a_link(self):
        cases = {
            # '#' would start the fragment: the browser never sends the rest.
            "Customer #12": "Customer%20%2312",
            # '?' would start the query string — /job/ABC, which is a 404.
            "ABC?7": "ABC%3F7",
            # A bare '%' is an invalid escape sequence.
            "50%": "50%25",
            # '/' would be a second path segment.
            "4711/03": "4711%2F03",
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(_path_segment(key), expected)

    def test_the_ungrouped_default_survives(self):
        """Without a pattern the key is the row identity — dots stay dots.

        Dots are unreserved in a URL, and the machine id leading the identity is
        restricted to URL-safe characters (config.MACHINE_ID_RE).
        """
        self.assertEqual(_path_segment("v1000-01.2026-08-14.189.29"), "v1000-01.2026-08-14.189.29")

    def test_a_plain_order_number_is_untouched(self):
        """The common case: no escape where none is needed."""
        self.assertEqual(_path_segment("105774"), "105774")

    def test_a_missing_key_yields_an_empty_segment(self):
        # The resulting /job/ URL returns 404.
        self.assertEqual(_path_segment(None), "")
        self.assertEqual(_path_segment(""), "")

    def test_a_machine_id_never_needs_escaping(self):
        """Machine ids are validated at load time, not escaped at use time —
        in the archive directory as well as in the identity fallback."""
        for machine_id in ("v1000-01", "press2", "IP_V1000"):
            with self.subTest(machine_id=machine_id):
                self.assertRegex(machine_id, config.MACHINE_ID_RE)
                self.assertEqual(_path_segment(machine_id), machine_id)


HOST = "pressledger.local:8000"


def _request(referer: str | None, host: str = HOST) -> Request:
    headers = [(b"host", host.encode())]
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/job/105774",
            "query_string": b"",
            "headers": headers,
            "server": ("127.0.0.1", 8000),
        }
    )


class BackLinkTests(unittest.TestCase):
    def test_the_filters_of_the_list_survive(self):
        self.assertEqual(
            _back_to_jobs(_request(f"http://{HOST}/jobs?jobtype=IP&sort=clicks")),
            "/jobs?jobtype=IP&sort=clicks",
        )

    def test_another_host_does_not_become_the_link(self):
        """The path alone matches on any domain that carries /jobs in it."""
        for referer in (
            "http://elsewhere.example/jobs",
            f"http://elsewhere.example/jobs?next=http://{HOST}/jobs",
            "https://elsewhere.example/redirect?to=/jobs",
        ):
            with self.subTest(referer=referer):
                self.assertEqual(_back_to_jobs(_request(referer)), "/jobs")

    def test_another_page_of_this_host_does_not_either(self):
        self.assertEqual(_back_to_jobs(_request(f"http://{HOST}/paper")), "/jobs")

    def test_no_referer_is_the_plain_list(self):
        # Followed from a bookmark, or a browser that sends none.
        self.assertEqual(_back_to_jobs(_request(None)), "/jobs")


if __name__ == "__main__":
    unittest.main()
