"""
Minimal Google Cloud Firestore client that talks to the Firestore REST API.

Only the standard library plus `cryptography` (to sign the service-account JWT) is used,
so no firebase-admin / google-cloud packages are needed.

Credentials: point GOOGLE_APPLICATION_CREDENTIALS at a Firebase service-account JSON key.
Keep that file OUTSIDE the project folder and never commit it.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SCOPE = "https://www.googleapis.com/auth/datastore"
API = "https://firestore.googleapis.com/v1"
METADATA = "http://metadata.google.internal/computeMetadata/v1"


class FirestoreError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class NotFound(FirestoreError):
    pass


class Conflict(FirestoreError):
    pass


class PreconditionFailed(FirestoreError):
    """A write was rejected because the document changed since we read it (optimistic concurrency)."""


def _map_error(http_status, api_status, message):
    if api_status == "NOT_FOUND" or http_status == 404:
        return NotFound(http_status, message)
    if api_status == "ALREADY_EXISTS":
        return Conflict(http_status, message)
    if api_status in ("FAILED_PRECONDITION", "ABORTED"):
        return PreconditionFailed(http_status, message)
    return FirestoreError(http_status, message)


def _send_text(url):
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode().strip()


def _send(req):
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        err = body.get("error", {})
        if isinstance(err, str):  # OAuth token endpoint style
            status, message = err, body.get("error_description", err)
        else:
            status, message = err.get("status", ""), err.get("message", str(e.reason))
        raise _map_error(e.code, status, message) from None
    except urllib.error.URLError as e:
        raise FirestoreError(503, f"Cannot reach Google services: {e.reason}") from None
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- value encoding
# Firestore stores typed values: {"stringValue": "x"}, {"integerValue": "5"}, ...

def to_value(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [to_value(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: to_value(x) for k, x in v.items()}}}
    raise TypeError(f"Unsupported value type: {type(v).__name__}")


def from_value(v):
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "timestampValue" in v:
        return v["timestampValue"]
    if "arrayValue" in v:
        return [from_value(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: from_value(x) for k, x in v["mapValue"].get("fields", {}).items()}
    return None


def to_fields(d):
    return {k: to_value(v) for k, v in d.items()}


def from_doc(doc):
    """Turn a Firestore document into a plain dict with 'id' and '_update_time' added."""
    out = {k: from_value(v) for k, v in doc.get("fields", {}).items()}
    out["id"] = doc["name"].rsplit("/", 1)[1]
    out["_update_time"] = doc.get("updateTime")
    return out


# ---------------------------------------------------------------- client

class Client:
    """credentials_path=None means: running on Google Cloud (e.g. Cloud Run), so use the
    service account attached to the service and the metadata server. No key file needed."""

    def __init__(self, credentials_path=None, credentials_info=None):
        """credentials_info: the service-account key as a dict (for hosts that take it as an env variable)."""
        self._key = None
        self.email = None
        self.token_uri = "https://oauth2.googleapis.com/token"
        if credentials_path or credentials_info:
            if credentials_info is None:
                with open(credentials_path, encoding="utf-8") as f:
                    credentials_info = json.load(f)
            info = credentials_info
            if info.get("type") != "service_account":
                raise ValueError("Credentials must be a Firebase service-account key (type: service_account).")
            self.project = info["project_id"]
            self.email = info["client_email"]
            self.token_uri = info.get("token_uri", self.token_uri)
            self._key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
        else:
            self.project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIREBASE_PROJECT_ID")
                            or self._metadata_project())
        self._docs = f"{API}/projects/{self.project}/databases/(default)/documents"
        self._db_path = f"projects/{self.project}/databases/(default)/documents"
        self._token = None
        self._token_exp = 0.0
        self._token_lock = threading.Lock()

    # -- auth
    @staticmethod
    def _metadata_project():
        return _send_text(f"{METADATA}/project/project-id")

    def _access_token(self):
        with self._token_lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            if self._key is None:  # on Google Cloud: ask the metadata server
                data = _send(urllib.request.Request(f"{METADATA}/instance/service-accounts/default/token",
                                                    headers={"Metadata-Flavor": "Google"}))
                self._token = data["access_token"]
                self._token_exp = time.time() + int(data.get("expires_in", 3600))
                return self._token
            now = int(time.time())
            b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")  # noqa: E731
            header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            claims = b64(json.dumps({
                "iss": self.email, "scope": SCOPE, "aud": self.token_uri, "iat": now, "exp": now + 3600,
            }).encode())
            signing_input = header + b"." + claims
            signature = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
            assertion = (signing_input + b"." + b64(signature)).decode()
            body = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
            }).encode()
            req = urllib.request.Request(self.token_uri, data=body,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            data = _send(req)
            self._token = data["access_token"]
            self._token_exp = now + int(data.get("expires_in", 3600))
            return self._token

    def _request(self, method, url, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        })
        return _send(req)

    # -- paths
    def _doc_url(self, coll, doc_id):
        return f"{self._docs}/{coll}/{urllib.parse.quote(doc_id, safe='')}"

    def _doc_name(self, coll, doc_id):
        return f"{self._db_path}/{coll}/{doc_id}"

    # -- reads
    def get(self, coll, doc_id):
        try:
            return from_doc(self._request("GET", self._doc_url(coll, doc_id)))
        except NotFound:
            return None

    def list_all(self, coll):
        out, token = [], None
        while True:
            params = {"pageSize": 300}
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{self._docs}/{coll}?{urllib.parse.urlencode(params)}")
            out.extend(from_doc(d) for d in data.get("documents", []))
            token = data.get("nextPageToken")
            if not token:
                return out

    def query(self, coll, where=None, order=None, limit=None):
        """where=(field, op, value) with op like 'EQUAL' or 'GREATER_THAN';
        order=(field, descending). Keep to one field so no composite index is needed."""
        sq = {"from": [{"collectionId": coll}]}
        if where:
            field, op, value = where
            sq["where"] = {"fieldFilter": {"field": {"fieldPath": field}, "op": op, "value": to_value(value)}}
        if order:
            field, descending = order
            sq["orderBy"] = [{"field": {"fieldPath": field},
                              "direction": "DESCENDING" if descending else "ASCENDING"}]
        if limit:
            sq["limit"] = limit
        rows = self._request("POST", f"{self._docs}:runQuery", {"structuredQuery": sq})
        return [from_doc(r["document"]) for r in rows if "document" in r]

    # -- writes
    def create(self, coll, fields, doc_id=None):
        url = f"{self._docs}/{coll}"
        if doc_id:
            url += "?" + urllib.parse.urlencode({"documentId": doc_id})
        return from_doc(self._request("POST", url, {"fields": to_fields(fields)}))

    def commit(self, writes):
        """Apply writes atomically. Raises PreconditionFailed if any precondition no longer holds."""
        return self._request("POST", f"{self._docs}:commit", {"writes": writes})

    def w_create(self, coll, doc_id, fields):
        return {"update": {"name": self._doc_name(coll, doc_id), "fields": to_fields(fields)},
                "currentDocument": {"exists": False}}

    def w_replace(self, coll, doc_id, fields, update_time):
        return {"update": {"name": self._doc_name(coll, doc_id), "fields": to_fields(fields)},
                "currentDocument": {"updateTime": update_time}}

    def w_increment(self, coll, doc_id, field, delta, update_time):
        return {"transform": {"document": self._doc_name(coll, doc_id),
                              "fieldTransforms": [{"fieldPath": field,
                                                   "increment": {"integerValue": str(delta)}}]},
                "currentDocument": {"updateTime": update_time}}

    def w_delete(self, coll, doc_id):
        return {"delete": self._doc_name(coll, doc_id), "currentDocument": {"exists": True}}
