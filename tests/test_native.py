import unittest
from unittest.mock import Mock

from xiaoetong_assistant.native import NativeClient


class NativeTests(unittest.TestCase):
    def client(self, responses):
        client = NativeClient.__new__(NativeClient)
        client.post = Mock(side_effect=responses)
        client.courseware = Mock(return_value=[])
        return client

    def test_sidebar_documents_are_included_without_signed_addresses(self):
        client = self.client([{"available":1,"subscribe":1,"list":[],"total":0}])
        client.courseware.return_value = [
            {"material_id":"notes","title":"Notes.pdf","type":9,"file_url":"https://example.invalid/notes.pdf?private=x"},
            {"material_id":"word","title":"Notes.docx","type":6,"file_url":"https://example.invalid/notes.docx"},
        ]
        items = client.resources({"resource_type":50,"resource_id":"c","app_id":"a","user_id":"u"})
        self.assertEqual([v.title for v in items], ["Notes.pdf","Notes.docx"])
        self.assertTrue(all(v.source == "courseware" and v.resource_type == 51 for v in items))
        self.assertNotIn("private=x", repr(items))
        self.assertIn(":courseware:", items[0].key)

    def test_removed_sidebar_document_is_denied(self):
        client = self.client([])
        with self.assertRaises(PermissionError):
            client.courseware_document("a", "u", "c", "removed")

    def test_paginates_all_authorized_course_rows(self):
        client = self.client([
            {"list": [{"app_id": "a", "resource_id": "1"}], "is_last": 0},
            {"list": [{"app_id": "a", "resource_id": "2"}], "is_last": 1},
        ])
        self.assertEqual(sum(map(len, client.course_pages())), 2)
        self.assertEqual(client.post.call_args_list[1].args[1]["page"], 2)
        self.assertEqual(client.post.call_args_list[0].args[1]["tab_type"], 0)

    def test_confirmed_empty_category_with_null_list(self):
        self.assertEqual(list(self.client([{"list": None, "total": 0, "is_last": 1}]).course_pages()), [[]])

    def test_repeated_course_page_is_incomplete(self):
        page = {"list": [{"app_id": "a", "resource_id": "1"}], "is_last": 0}
        with self.assertRaises(RuntimeError):
            list(self.client([page, page]).course_pages())

    def test_unknown_pagination_not_claimed_complete(self):
        with self.assertRaises(ValueError):
            list(self.client([{"list": [{"resource_id": "1"}]}]).course_pages())

    def test_server_denial_preserved_without_logging_private_message(self):
        with self.assertRaises(PermissionError) as failure:
            NativeClient.unwrap({"code": 403, "msg": "private-response", "data": {}})
        self.assertNotIn("private-response", str(failure.exception))

    def test_nested_catalog_and_multiple_pages_keep_video_document_types(self):
        def page(rows, total):
            return {"available":1,"subscribe":1,"list":rows,"total":total}
        folder = {"chapter_type":1,"chapter_id":"folder"}
        video = {"chapter_type":2,"chapter_id":"v","resource_title":"Video","resource_type":3,"unlock_state":1,"is_try":0}
        document = {"chapter_type":2,"chapter_id":"d","resource_title":"Document","resource_type":51,"unlock_state":1}
        client = self.client([page([folder],1),page([video],2),page([document],2)])
        items = client.resources({"resource_type":50,"resource_id":"course","app_id":"a","user_id":"u"})
        self.assertEqual([item.resource_type for item in items],[3,51])
        self.assertEqual(client.post.call_args_list[2].args[1]["buz_data"]["page"],2)
        self.assertEqual(client.post.call_args_list[1].args[1]["buz_data"]["p_id"],"folder")

    def test_catalog_access_denial_is_not_an_empty_authorized_course(self):
        client = self.client([{"available":0,"subscribe":1,"list":[],"total":0}])
        with self.assertRaises(PermissionError):
            client.resources({"resource_type":50,"resource_id":"c","app_id":"a","user_id":"u"})

    def test_catalog_truncation_is_not_claimed_complete(self):
        client = self.client([{"available":1,"subscribe":1,"list":[],"total":2}])
        with self.assertRaises(ValueError):
            client.resources({"resource_type":50,"resource_id":"c","app_id":"a","user_id":"u"})
