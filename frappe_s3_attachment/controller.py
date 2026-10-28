"""S3-Ablage für Frappe-Dateien (File-DocType).

corevatis-v16: S3-kompatible Endpunkte (RustFS/MinIO/Ceph RGW) über
``s3_endpoint_url``, Konfiguration wahlweise aus der site_config (Vorrang) oder
dem Single-DocType "S3 File Attachment", alle Objekte privat im Bucket,
Auslieferung über die Site (Streaming mit Rechteprüfung) statt Presigned-Redirect
auf einen evtl. nur intern erreichbaren Endpunkt.

site_config-Schlüssel:
    s3_bucket, s3_access_key, s3_secret_key   (Pflicht, sonst DocType)
    s3_endpoint_url                           z. B. https://mvp-s3.corevatis.de
    s3_region                                 Standard us-east-1
    s3_folder                                 Präfix, Standard: Site-Name
    s3_stream_downloads                       1 (Standard) = über die Site streamen,
                                              0 = Redirect auf Presigned URL
    s3_signed_url_expiry                      Sekunden, Standard 120
    s3_delete_from_cloud                      1 = beim Löschen in Frappe auch in S3
                                              löschen (bei Versionierung: Delete-Marker)
    ignore_s3_upload_for_doctype              Liste, Standard ["Data Import"]
    s3_allow_guest_downloads                  1 = öffentliche Dateien (is_private=0) auch
                                              ohne Login ausliefern; Standard 0 = Download
                                              nur für angemeldete Benutzer (Session/Token)
    s3_guest_download_rate_limit              Gäste-Downloads je IP und Minute, Standard 60
    s3_inline_max_bytes                       größere Dateien kommen immer als Attachment,
                                              Standard 20 MiB
    s3_connect_timeout / s3_read_timeout      boto-Timeouts in Sekunden, Standard 5 / 30

Download (``generate_file``): das S3-Objekt wird in Blöcken (``STREAM_CHUNK_SIZE``) zum
Client gestreamt, nie komplett in den Worker geladen; ``Content-Length`` kommt aus dem
Objekt. Gäste bekommen ohne ``s3_allow_guest_downloads`` immer 403 - auch für öffentliche
Dateien; mit dem Schalter nur öffentliche Dateien, mit Rate-Limit je IP. Für angemeldete
Benutzer gilt: öffentliche Datei = für jeden angemeldeten Benutzer lesbar, private Datei
nur mit Leserecht auf jeden File-Datensatz zum Schlüssel.
"""

import datetime
import mimetypes
import os
import random
import re
import string

import boto3
import frappe
from botocore.client import Config
from botocore.exceptions import ClientError
from frappe.rate_limiter import rate_limit
from werkzeug.wrappers import Response

GENERATE_FILE = "/api/method/frappe_s3_attachment.controller.generate_file"

# Nur diese Typen werden inline (im Browser) angezeigt; alles andere – auch
# HTML und SVG – kommt als Attachment mit application/octet-stream, damit kein
# hochgeladener Inhalt auf dem Site-Origin ausgeführt wird (Stored XSS).
INLINE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


STREAM_CHUNK_SIZE = 64 * 1024
INLINE_MAX_BYTES = 20 * 1024 * 1024
GUEST_RATE_LIMIT = 60


def _conf():
    return frappe.local.conf


def _session_user():
    return frappe.session.user


def ensure_custom_fields():
    """Feld File.s3_key (after_install/after_migrate): der S3-Schlüssel steht
    in einem eigenen, schreibgeschützten Feld statt in content_hash."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(
        {
            "File": [
                {
                    "fieldname": "s3_key",
                    "label": "S3 Key",
                    "fieldtype": "Data",
                    "length": 512,
                    "read_only": 1,
                    "hidden": 1,
                    "permlevel": 1,
                    "search_index": 1,
                    "insert_after": "content_hash",
                }
            ]
        },
        update=True,
    )


def _settings():
    """Einstellungen: site_config hat Vorrang vor dem DocType."""
    conf = frappe.local.conf
    if conf.get("s3_bucket"):
        return frappe._dict(
            bucket_name=conf.get("s3_bucket"),
            aws_key=conf.get("s3_access_key"),
            aws_secret=conf.get("s3_secret_key"),
            region_name=conf.get("s3_region") or "us-east-1",
            endpoint_url=conf.get("s3_endpoint_url"),
            folder_name=conf.get("s3_folder", frappe.local.site),
            signed_url_expiry_time=conf.get("s3_signed_url_expiry") or 120,
            delete_file_from_cloud=conf.get("s3_delete_from_cloud", 0),
            stream_downloads=conf.get("s3_stream_downloads", 1),
        )
    doc = frappe.get_single("S3 File Attachment")
    return frappe._dict(
        bucket_name=doc.bucket_name,
        aws_key=doc.aws_key,
        aws_secret=doc.aws_secret,
        region_name=doc.region_name or "us-east-1",
        endpoint_url=doc.get("endpoint_url") or None,
        folder_name=doc.folder_name,
        signed_url_expiry_time=doc.signed_url_expiry_time or 120,
        delete_file_from_cloud=doc.delete_file_from_cloud,
        stream_downloads=conf.get("s3_stream_downloads", 1),
    )


def is_configured():
    s = _settings()
    return bool(s.bucket_name)


class S3Operations:
    def __init__(self):
        self.s3_settings_doc = _settings()
        kwargs = dict(
            region_name=self.s3_settings_doc.region_name,
            # Pfad-Adressierung: S3-kompatible Server ohne Wildcard-DNS je Bucket
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=frappe.utils.cint(_conf().get("s3_connect_timeout")) or 5,
                read_timeout=frappe.utils.cint(_conf().get("s3_read_timeout")) or 30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        if self.s3_settings_doc.endpoint_url:
            kwargs["endpoint_url"] = self.s3_settings_doc.endpoint_url
        if self.s3_settings_doc.aws_key and self.s3_settings_doc.aws_secret:
            kwargs["aws_access_key_id"] = self.s3_settings_doc.aws_key
            kwargs["aws_secret_access_key"] = self.s3_settings_doc.aws_secret
        self.S3_CLIENT = boto3.client("s3", **kwargs)
        self.BUCKET = self.s3_settings_doc.bucket_name
        self.folder_name = self.s3_settings_doc.folder_name

    def strip_special_chars(self, file_name):
        return re.sub("[^0-9a-zA-Z._-]", "", file_name)

    def key_generator(self, file_name, parent_doctype, parent_name):
        hook_cmd = frappe.get_hooks().get("s3_key_generator")
        if hook_cmd:
            try:
                k = frappe.get_attr(hook_cmd[0])(
                    file_name=file_name, parent_doctype=parent_doctype, parent_name=parent_name
                )
                if k:
                    return k.rstrip("/").lstrip("/")
            except Exception:
                frappe.log_error("s3_key_generator failed")

        file_name = self.strip_special_chars(file_name.replace(" ", "_"))
        key = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        today = datetime.datetime.now()
        parts = [today.strftime("%Y"), today.strftime("%m"), today.strftime("%d"),
                 self.strip_special_chars(parent_doctype.replace(" ", "_")), f"{key}_{file_name}"]
        if self.folder_name:
            parts.insert(0, self.folder_name.strip("/"))
        return "/".join(parts)

    def upload_files_to_s3_with_key(self, file_path, file_name, is_private, parent_doctype, parent_name):
        """Lädt eine Datei hoch. Objekte sind immer privat (kein public-read-ACL);
        öffentliche Frappe-Dateien liefert generate_file angemeldeten Benutzern (Gästen nur
        mit s3_allow_guest_downloads) aus."""
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        key = self.key_generator(file_name, parent_doctype, parent_name)
        try:
            self.S3_CLIENT.upload_file(
                file_path,
                self.BUCKET,
                key,
                ExtraArgs={"ContentType": content_type, "Metadata": {"file_name": _ascii(file_name)}},
            )
        except (boto3.exceptions.S3UploadFailedError, ClientError):
            frappe.log_error("S3 upload failed")
            frappe.throw(frappe._("File Upload Failed. Please try again."))
        return key

    def delete_from_s3(self, key):
        if self.s3_settings_doc.delete_file_from_cloud:
            try:
                self.S3_CLIENT.delete_object(Bucket=self.BUCKET, Key=key)
            except ClientError:
                frappe.throw(frappe._("Access denied: Could not delete file"))

    def read_file_from_s3(self, key):
        return self.S3_CLIENT.get_object(Bucket=self.BUCKET, Key=key)

    def get_url(self, key, file_name=None):
        params = {"Bucket": self.BUCKET, "Key": key}
        if file_name:
            params["ResponseContentDisposition"] = f"filename={_ascii(file_name)}"
        return self.S3_CLIENT.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=int(self.s3_settings_doc.signed_url_expiry_time)
        )


def _ascii(value):
    return value.encode("ascii", "ignore").decode() or "file"


def _file_url(key, file_name):
    return f"{GENERATE_FILE}?key={frappe.utils.quote(key, safe='/')}&file_name={frappe.utils.quote(file_name or '')}"


def file_upload_to_s3(doc, method):
    """after_insert von File: Inhalt nach S3, lokale Kopie entfernen."""
    if doc.is_folder or not doc.file_url or doc.file_url.startswith(("http://", "https://", GENERATE_FILE)):
        return
    if not is_configured():
        return
    parent_doctype = doc.attached_to_doctype or "File"
    ignore = frappe.local.conf.get("ignore_s3_upload_for_doctype") or ["Data Import"]
    if parent_doctype in ignore:
        return

    s3_upload = S3Operations()
    site_path = frappe.utils.get_site_path()
    file_path = (site_path + doc.file_url) if doc.is_private else (site_path + "/public" + doc.file_url)
    if not os.path.exists(file_path):
        return
    key = s3_upload.upload_files_to_s3_with_key(
        file_path, doc.file_name, doc.is_private, parent_doctype, doc.attached_to_name
    )
    file_url = _file_url(key, doc.file_name)
    os.remove(file_path)
    frappe.db.sql(
        """UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, s3_key=%s WHERE name=%s""",
        (file_url, "Home/Attachments", "Home/Attachments", key, doc.name),
    )
    doc.file_url = file_url
    doc.s3_key = key

    if parent_doctype and doc.attached_to_name and frappe.get_meta(parent_doctype).get("image_field"):
        image_field = frappe.get_meta(parent_doctype).get("image_field")
        if not frappe.db.get_value(parent_doctype, doc.attached_to_name, image_field):
            frappe.db.set_value(parent_doctype, doc.attached_to_name, image_field, file_url)


def _file_rows_for_key(key):
    """Alle File-Datensätze zu einem S3-Schlüssel (eigenes Feld; Altbestand
    der upstream-Version: Schlüssel in content_hash)."""
    fields = ["name", "is_private", "file_name"]
    rows = frappe.get_all("File", filters={"s3_key": key, "file_url": ["like", GENERATE_FILE + "%"]}, fields=fields)
    if not rows:
        rows = frappe.get_all(
            "File", filters={"content_hash": key, "file_url": ["like", GENERATE_FILE + "%"]}, fields=fields
        )
    return rows


def check_read_access(key):
    """Liefert den File-Datensatz, über den ausgeliefert wird, oder wirft
    PermissionError. Abgelehnt wird, sobald IRGENDEIN Datensatz zu diesem
    Schlüssel privat und für den aktuellen Nutzer nicht lesbar ist – ein
    gefälschter oder kopierter Datensatz kann so nie Zugriff verschaffen,
    höchstens verweigern."""
    rows = _file_rows_for_key(key)
    if not rows:
        raise frappe.PermissionError
    docs = [frappe.get_doc("File", r.name) for r in rows]
    for d in docs:
        if d.is_private and not d.has_permission("read"):
            raise frappe.PermissionError
    return docs[0]


def _inline_max_bytes():
    value = _conf().get("s3_inline_max_bytes")
    return INLINE_MAX_BYTES if value in (None, "") else frappe.utils.cint(value)


def build_download_response(file_name, body, content_length=None):
    """Response mit sicheren Headern: Content-Type serverseitig aus der
    Dateiendung (Allowlist), sonst Attachment + octet-stream. Dateien über
    ``s3_inline_max_bytes`` kommen immer als Attachment. ``body``: Bytes oder
    Iterator von Blöcken (Streaming)."""
    ext = os.path.splitext(file_name or "")[1].lower()
    inline_type = INLINE_TYPES.get(ext)
    if inline_type and content_length is not None and content_length > _inline_max_bytes():
        inline_type = None
    streamed = not isinstance(body, bytes | bytearray | str)
    response = Response(body, direct_passthrough=streamed)
    if content_length is not None:
        response.content_length = content_length
    response.mimetype = inline_type or "application/octet-stream"
    response.headers.add(
        "Content-Disposition", "inline" if inline_type else "attachment", filename=file_name or "file"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
    response.headers["Cache-Control"] = "private, max-age=0, no-store"
    return response


def stream_body(body, chunk_size=STREAM_CHUNK_SIZE):
    """Blöcke aus dem botocore-StreamingBody; schließt die Verbindung am Ende
    (auch bei Abbruch durch den Client)."""
    try:
        yield from body.iter_chunks(chunk_size=chunk_size)
    finally:
        body.close()


@rate_limit(
    limit=lambda: frappe.utils.cint(_conf().get("s3_guest_download_rate_limit")) or GUEST_RATE_LIMIT,
    seconds=60,
    ip_based=True,
    endpoint="frappe_s3_attachment.controller.generate_file:guest",
)
def _count_guest_download():
    return None


def check_guest_access():
    """Gäste nur, wenn ``s3_allow_guest_downloads`` gesetzt ist (dann mit Rate-Limit je IP)."""
    if _session_user() not in (None, "", "Guest"):
        return
    if not frappe.utils.cint(_conf().get("s3_allow_guest_downloads")):
        raise frappe.PermissionError
    _count_guest_download()


# allow_guest, damit öffentliche Dateien per Schalter erreichbar sein können; ohne
# Schalter weist check_guest_access jeden Gast ab (vor jedem DB-/S3-Zugriff).
@frappe.whitelist(allow_guest=True)
def generate_file(key=None, file_name=None):
    """Liefert eine S3-Datei aus (gestreamt). Private Dateien nur mit Leserecht auf
    jeden File-Datensatz zu diesem Schlüssel; öffentliche für angemeldete Benutzer,
    für Gäste nur mit ``s3_allow_guest_downloads``."""
    check_guest_access()
    if not key:
        raise frappe.DoesNotExistError
    file_doc = check_read_access(key)

    s3_upload = S3Operations()
    if not frappe.utils.cint(s3_upload.s3_settings_doc.stream_downloads):
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = s3_upload.get_url(key, file_doc.file_name)
        return

    try:
        obj = s3_upload.read_file_from_s3(key)
    except ClientError:
        raise frappe.DoesNotExistError
    length = obj.get("ContentLength")
    return build_download_response(
        file_doc.file_name, stream_body(obj["Body"]), None if length is None else int(length)
    )


def upload_existing_files_s3(name):
    """Eine vorhandene lokale Datei nach S3 verschieben."""
    if not frappe.db.exists("File", name):
        return
    file_upload_to_s3(frappe.get_doc("File", name), None)


def s3_file_regex_match(file_url):
    return re.match(r"^(https?:|/api/method/frappe_s3_attachment.controller.generate_file)", file_url)


@frappe.whitelist()
def migrate_existing_files():
    frappe.only_for("System Manager")
    for file in frappe.get_all("File", fields=["name", "file_url"], filters={"is_folder": 0}):
        if file["file_url"] and not s3_file_regex_match(file["file_url"]):
            upload_existing_files_s3(file["name"])
    return True


def delete_from_cloud(doc, method):
    if not (doc.file_url and doc.file_url.startswith(GENERATE_FILE) and is_configured()):
        return
    key = doc.get("s3_key") or doc.content_hash
    others = [r for r in _file_rows_for_key(key) if r.name != doc.name]
    if key and not others:
        S3Operations().delete_from_s3(key)


@frappe.whitelist()
def ping():
    return "pong"
