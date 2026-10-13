"""Authenticated loopback adapter for the user's own running client session."""
import json
from pathlib import Path
from dataclasses import dataclass

from .network import make_session


@dataclass
class NativeResource:
    app_id: str
    user_id: str
    course_id: str
    resource_id: str
    title: str
    course_title: str
    resource_type: int
    unlocked: bool
    trial: bool
    source: str = "catalog"

    @property
    def key(self):
        if self.source == "courseware":
            return f"{self.app_id}:{self.course_id}:courseware:{self.resource_id}"
        return f"{self.app_id}:{self.course_id}:{self.resource_id}"

    def video_identity(self):
        return {key:getattr(self,key) for key in ("app_id","user_id","course_id","resource_id")}


class NativeClient:
    def __init__(self, project):
        descriptor = Path(project) / "runtime" / "native-bridge.json"
        if not descriptor.is_file():
            raise ConnectionError("Native bridge is not active; install the patch and restart the client")
        info = json.loads(descriptor.read_text(encoding="utf-8"))
        port = info.get("port")
        token = info.get("token")
        if info.get("protocol") != 1 or type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or len(token) != 64:
            raise ValueError("Invalid native bridge descriptor")
        self.url = f"http://127.0.0.1:{port}/v1"
        self.session = make_session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def close(self):
        self.session.close()

    def call(self, action, **fields):
        with self.session.post(self.url, json={"action": action, **fields}, timeout=(3, 30), allow_redirects=False) as response:
            if response.status_code != 200:
                try:
                    reason = response.json().get("error", "unknown")
                except ValueError:
                    reason = "invalid_response"
                if reason not in {"unsupported_request", "trial_video", "fetch_failed", "native_request_failed", "unauthorized", "invalid_response"}:
                    reason = "unknown"
                raise RuntimeError(f"Native bridge returned HTTP {response.status_code} ({reason})")
            result = response.json()
        if result.get("error"):
            raise RuntimeError(f"Native client failure: {result['error']}")
        return result

    def status(self):
        return self.call("status")

    def post(self, endpoint, body):
        payload = self.call("post", endpoint=endpoint, body=body).get("payload")
        return self.unwrap(payload)

    @staticmethod
    def unwrap(payload):
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            code = payload.get("code") if isinstance(payload, dict) else "missing"
            # No raw server messages, tokens, or signed URLs in exception output.
            raise PermissionError(f"Client API did not succeed (code={code})")
        if not isinstance(payload.get("data"), dict):
            raise ValueError("Unsupported client API data")
        return payload["data"]

    def course_pages(self, tab_type=0, page_size=20):
        endpoint = "/xe.pc_client.course/my.all.course.lists.get/3.0.1"
        seen = set()
        for page in range(1, 101):
            data = self.post(endpoint, {"tab_type": tab_type, "page": page, "page_size": page_size})
            rows = data.get("list")
            if rows is None and data.get("total") == 0 and data.get("is_last") in (True, 1, "1", "true"):
                rows = []
            if not isinstance(rows, list):
                raise ValueError("Unsupported course list")
            signature = tuple((str(row.get("app_id", "")), str(row.get("resource_id", ""))) for row in rows)
            if rows and signature in seen:
                raise RuntimeError("Repeated course page; enumeration is incomplete")
            seen.add(signature)
            yield rows
            if data.get("is_last") in (True, 1, "1", "true") or not rows:
                return
            if "is_last" not in data:
                raise ValueError("Missing course pagination end marker")
        raise RuntimeError("Course pagination limit reached")

    def video(self, app_id, user_id, resource_id, course_id=""):
        return self.unwrap(self.call("video", app_id=app_id, user_id=user_id, resource_id=resource_id, course_id=course_id).get("payload"))

    def document(self, **identity):
        return self.unwrap(self.call("document", **identity).get("payload"))

    def courseware(self, app_id, user_id, course_id):
        data = self.unwrap(self.call("courseware", app_id=app_id, user_id=user_id, course_id=course_id).get("payload"))
        rows = data.get("list")
        if not isinstance(rows, list):
            raise ValueError("Unsupported course materials list")
        return rows

    def courseware_document(self, app_id, user_id, course_id, resource_id):
        for row in self.courseware(app_id, user_id, course_id):
            if row.get("material_id") == resource_id:
                return {"file_url": row["file_url"], "file_name": row["title"]}
        raise PermissionError("Course material is no longer available")

    def resources(self, course):
        """Enumerate the observed type-50 course tree without treating folders as videos."""
        if int(course.get("resource_type", 0)) != 50:
            raise ValueError("Course type is not yet supported by the native catalog adapter")
        endpoint = "/xe.pc_client.course.business.avoidlogin.e_course.resource_catalog_list.get/1.0.0"
        course_id = str(course["resource_id"])
        app_id, user_id = str(course["app_id"]), str(course["user_id"])
        seen_folders, resources = set(), {}

        def walk(parent="0", sub_course="", depth=0):
            identity = (parent, sub_course)
            if identity in seen_folders or depth > 12:
                raise ValueError("Repeated or excessively nested native catalog folder")
            seen_folders.add(identity)
            count, seen_rows = 0, set()
            for number in range(1, 101):
                data = self.post(endpoint, {"app_id":app_id,"user_id":user_id,"buz_data":{
                    "resource_id":"","course_id":course_id,"p_id":parent,"order":"asc",
                    "page":number,"page_size":20,"sub_course_id":sub_course,
                }})
                if data.get("available") not in (True,1,"1") or data.get("subscribe") not in (True,1,"1"):
                    raise PermissionError("Native course access is unavailable")
                rows, total = data.get("list"), data.get("total")
                if not isinstance(rows,list) or type(total) is not int or total < 0:
                    raise ValueError("Unsupported native chapter pagination")
                for row in rows:
                    row_id = (str(row.get("chapter_id","")),str(row.get("resource_id","")))
                    if row_id in seen_rows:
                        raise ValueError("Repeated chapter page; catalog is incomplete")
                    seen_rows.add(row_id)
                    kind = int(row.get("chapter_type",0))
                    if kind == 1:
                        folder_id = str(row.get("chapter_id") or "")
                        if not folder_id:
                            raise ValueError("Missing native folder identity")
                        walk(folder_id,str(row.get("sub_course_id") or sub_course),depth+1)
                    elif kind == 2:
                        resource_id = str(row.get("resource_id") or row.get("chapter_id") or "")
                        title = str(row.get("resource_title") or row.get("chapter_title") or "")
                        if not resource_id or not title:
                            raise ValueError("Missing native resource identity or title")
                        resource = NativeResource(app_id,user_id,course_id,resource_id,title,str(course.get("title",course_id)),int(row.get("resource_type",0)),row.get("unlock_state") in (True,1,"1"),row.get("is_try") in (True,1,"1"))
                        resources[resource.key] = resource
                    else:
                        raise ValueError("Unknown native chapter node type")
                count += len(rows)
                if count >= total:
                    if count != total:
                        raise ValueError("Native chapter total does not match row count")
                    return
                if not rows:
                    raise ValueError("Empty chapter page before the advertised total")
            raise ValueError("Chapter pagination limit reached")

        walk()
        # Sidebar attachments are independent of the chapter catalog. Keep their
        # signed addresses out of catalog objects; resolve again at download time.
        for row in self.courseware(app_id, user_id, course_id):
            identity, title = row.get("material_id"), row.get("title")
            if not isinstance(identity, str) or not identity or not isinstance(title, str) or not title:
                raise ValueError("Missing course material identity or title")
            if not title.lower().endswith((".pdf", ".doc", ".docx")):
                if row.get("type") == 9:
                    title += ".pdf"
                else:
                    continue
            resource = NativeResource(app_id,user_id,course_id,identity,title,str(course.get("title",course_id)),51,True,False,"courseware")
            resources[resource.key] = resource
        return list(resources.values())
