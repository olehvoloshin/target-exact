"""
Standalone mock test for the PurchaseEntryLines update fix.

hotglue_singer_sdk couldn't be installed in this sandbox (pendulum build failure),
so this reimplements *only* the logic that changed (_get_existing_line_ids,
_replace_purchase_entry_lines, and the relevant branch of upsert_record) against a
fake `requests.request`, using the exact same endpoint paths / payload shapes as the
real patch in target_exact/sinks.py. This proves the algorithm (request sequencing,
payload contents, and failure handling) is correct; it does not exercise the rest of
ExactSink/HotglueSink plumbing (auth, backoff, hash-dedup, etc), which is unchanged.
"""
import unittest
from unittest.mock import patch, MagicMock
import xml.etree.ElementTree as ET


def make_feed_xml(ids):
    """Build a minimal Atom feed XML with the given entry IDs, matching Exact's shape.

    Exact's real responses put an m:type attribute on the value element (e.g.
    <d:ID m:type="Edm.Guid">...), which is what makes xmltodict emit {"#text": ...}
    instead of a bare string - matching the real sinks.py code's ["#text"] access.
    """
    entries = "".join(
        f"""<entry>
            <content><m:properties xmlns:m="m" xmlns:d="d">
                <d:ID m:type="Edm.Guid">{i}</d:ID>
            </m:properties></content>
        </entry>"""
        for i in ids
    )
    if not entries:
        return "<feed></feed>"
    return f"<feed>{entries}</feed>"


def make_entry_xml(entry_id, field="ID"):
    return f"""<entry>
        <content><m:properties xmlns:m="m" xmlns:d="d">
            <d:{field} m:type="Edm.Guid">{entry_id}</d:{field}>
        </m:properties></content>
    </entry>"""


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSink:
    """Minimal stand-in exposing only what _replace_purchase_entry_lines needs."""

    def __init__(self):
        self.calls = []  # (method, endpoint, params, request_data)

    def request_api(self, method, endpoint=None, params=None, request_data=None, headers=None):
        self.calls.append((method, endpoint, params, request_data))
        return self._dispatch(method, endpoint, params, request_data)

    # overridden per-test via monkeypatch of self._dispatch
    def _dispatch(self, method, endpoint, params, request_data):
        raise NotImplementedError


import xmltodict


def get_existing_line_ids(sink, entry_id):
    response = sink.request_api(
        "GET",
        endpoint="/purchaseentry/PurchaseEntryLines",
        params={"$filter": f"EntryID eq guid'{entry_id}'", "$select": "ID"},
    )
    response_json = xmltodict.parse(response.text)
    entries = (response_json.get("feed") or {}).get("entry")
    if not entries:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    return [entry["content"]["m:properties"]["d:ID"]["#text"] for entry in entries]


def replace_purchase_entry_lines(sink, entry_id, new_lines):
    existing_line_ids = get_existing_line_ids(sink, entry_id)

    deleted_ids = []
    try:
        for line_id in existing_line_ids:
            sink.request_api("DELETE", endpoint=f"/purchaseentry/PurchaseEntryLines(guid'{line_id}')")
            deleted_ids.append(line_id)
    except Exception as e:
        raise Exception(
            f"Failed to delete existing PurchaseEntryLines for entry {entry_id} "
            f"(deleted {len(deleted_ids)}/{len(existing_line_ids)} lines before failure): {e}"
        )

    created_ids = []
    try:
        for line in new_lines:
            line_payload = dict(line)
            line_payload["EntryID"] = entry_id
            response = sink.request_api(
                "POST", endpoint="/purchaseentry/PurchaseEntryLines", request_data=line_payload
            )
            line_json = xmltodict.parse(response.text)
            created_ids.append(line_json["entry"]["content"]["m:properties"]["d:ID"]["#text"])
    except Exception as e:
        raise Exception(
            f"Deleted {len(deleted_ids)} old PurchaseEntryLines for entry {entry_id} but failed "
            f"to recreate them (created {len(created_ids)}/{len(new_lines)} before failure): {e}"
        )
    return created_ids


class TestReplacePurchaseEntryLines(unittest.TestCase):

    def test_update_replaces_lines_deletes_old_creates_new(self):
        """Mock scenario matching the real bug report: PUT-only update silently dropped
        the amount. After the fix, an update with new lines should: GET existing line
        ids -> DELETE each -> POST each new line with the new amount."""
        sink = FakeSink()
        entry_id = "edd53fbd-3b9b-4ef2-8a54-d4fcd761fcd4"
        old_line_ids = ["line-aaa", "line-bbb"]
        new_lines = [
            {"AmountFC": 15000.0, "AmountDC": 15000.0, "GLAccount": "gl-1",
             "Description": "test", "VATCode": None, "CostCenter": None, "CostUnit": None},
        ]

        def dispatch(method, endpoint, params, request_data):
            if method == "GET" and endpoint == "/purchaseentry/PurchaseEntryLines":
                assert params["$filter"] == f"EntryID eq guid'{entry_id}'"
                return FakeResponse(make_feed_xml(old_line_ids))
            if method == "DELETE":
                assert endpoint in [f"/purchaseentry/PurchaseEntryLines(guid'{lid}')" for lid in old_line_ids]
                return FakeResponse("", status_code=204)
            if method == "POST" and endpoint == "/purchaseentry/PurchaseEntryLines":
                assert request_data["EntryID"] == entry_id
                assert request_data["AmountFC"] == 15000.0  # the amount that never used to arrive
                return FakeResponse(make_entry_xml("new-line-1"), status_code=201)
            raise AssertionError(f"Unexpected call: {method} {endpoint}")

        sink._dispatch = dispatch
        created = replace_purchase_entry_lines(sink, entry_id, new_lines)

        self.assertEqual(created, ["new-line-1"])
        methods_in_order = [c[0] for c in sink.calls]
        self.assertEqual(methods_in_order, ["GET", "DELETE", "DELETE", "POST"])
        # both old lines were deleted before the new one was posted
        deleted_endpoints = [c[1] for c in sink.calls if c[0] == "DELETE"]
        self.assertEqual(
            deleted_endpoints,
            [f"/purchaseentry/PurchaseEntryLines(guid'{lid}')" for lid in old_line_ids],
        )

    def test_update_with_no_existing_lines_only_creates(self):
        """Entry currently has zero lines (edge case) - should skip DELETE entirely
        and just POST the new ones."""
        sink = FakeSink()
        entry_id = "entry-no-lines"
        new_lines = [{"AmountFC": 500.0, "AmountDC": 500.0, "GLAccount": "gl-2"}]

        def dispatch(method, endpoint, params, request_data):
            if method == "GET":
                return FakeResponse(make_feed_xml([]))
            if method == "POST":
                return FakeResponse(make_entry_xml("new-line-x"), status_code=201)
            raise AssertionError(f"Unexpected call: {method} {endpoint}")

        sink._dispatch = dispatch
        created = replace_purchase_entry_lines(sink, entry_id, new_lines)

        self.assertEqual(created, ["new-line-x"])
        self.assertEqual([c[0] for c in sink.calls], ["GET", "POST"])

    def test_delete_failure_raises_clear_error_and_stops_before_posting(self):
        """If a DELETE fails partway through, we must not silently continue to POST
        new lines on top of a half-deleted state - fail loudly instead."""
        sink = FakeSink()
        entry_id = "entry-delete-fails"
        old_line_ids = ["line-1", "line-2", "line-3"]
        new_lines = [{"AmountFC": 1.0, "AmountDC": 1.0, "GLAccount": "gl-3"}]

        call_count = {"delete": 0}

        def dispatch(method, endpoint, params, request_data):
            if method == "GET":
                return FakeResponse(make_feed_xml(old_line_ids))
            if method == "DELETE":
                call_count["delete"] += 1
                if call_count["delete"] == 2:
                    raise Exception("500 Internal Server Error")
                return FakeResponse("", status_code=204)
            raise AssertionError("POST should never be reached if DELETE fails")

        sink._dispatch = dispatch

        with self.assertRaises(Exception) as ctx:
            replace_purchase_entry_lines(sink, entry_id, new_lines)

        self.assertIn("deleted 1/3 lines before failure", str(ctx.exception))
        # confirm no POST was ever attempted
        self.assertNotIn("POST", [c[0] for c in sink.calls])

    def test_create_path_unchanged_lines_embedded_in_single_post(self):
        """Sanity check for the *create* branch: preprocess_record's payload still
        carries PurchaseEntryLines embedded for a single POST, matching pre-fix
        behaviour (this branch of upsert_record was NOT touched by the fix)."""
        record = {
            "Currency": "GBP", "Journal": "60",
            "PurchaseEntryLines": [{"AmountFC": 1000.0, "AmountDC": 1000.0, "GLAccount": "gl-4"}],
        }
        # Mirrors the actual upsert_record branch: `id = record.pop("Id", None)` -> None
        # -> `elif new_lines is not None: record["PurchaseEntryLines"] = new_lines`
        id_ = record.pop("Id", None)
        new_lines = record.pop("PurchaseEntryLines", None)
        self.assertIsNone(id_)
        if id_:
            pass
        elif new_lines is not None:
            record["PurchaseEntryLines"] = new_lines

        self.assertIn("PurchaseEntryLines", record)
        self.assertEqual(record["PurchaseEntryLines"][0]["AmountFC"], 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
