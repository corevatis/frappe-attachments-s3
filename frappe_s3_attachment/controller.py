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

GENERATE_FILE = "/api/method/frappe_s3_attachment.controller.generate_file"


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
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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
        öffentliche Frappe-Dateien liefert generate_file ohne Login aus."""
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
    return f"{GENERATE_FILE}?key={key}&file_name={frappe.utils.quote(file_name or '')}"


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
        """UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, content_hash=%s WHERE name=%s""",
        (file_url, "Home/Attachments", "Home/Attachments", key, doc.name),
    )
    doc.file_url = file_url
    doc.content_hash = key

    if parent_doctype and doc.attached_to_name and frappe.get_meta(parent_doctype).get("image_field"):
        image_field = frappe.get_meta(parent_doctype).get("image_field")
        if not frappe.db.get_value(parent_doctype, doc.attached_to_name, image_field):
            frappe.db.set_value(parent_doctype, doc.attached_to_name, image_field, file_url)


@frappe.whitelist(allow_guest=True)
def generate_file(key=None, file_name=None):
    """Liefert eine S3-Datei aus — nur mit Leserecht auf den File-Datensatz
    (öffentliche Dateien ohne Login, private nach Frappe-Rechten)."""
    if not key:
        frappe.throw(frappe._("Key not found."), frappe.DoesNotExistError)

    file_name_db = frappe.db.get_value("File", {"content_hash": key}, "name")
    if not file_name_db:
        raise frappe.PermissionError
    file_doc = frappe.get_doc("File", file_name_db)
    if file_doc.is_private and not file_doc.has_permission("read"):
        raise frappe.PermissionError

    s3_upload = S3Operations()
    if not frappe.utils.cint(s3_upload.s3_settings_doc.stream_downloads):
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = s3_upload.get_url(key, file_doc.file_name)
        return

    try:
        obj = s3_upload.read_file_from_s3(key)
    except ClientError:
        raise frappe.DoesNotExistError
    frappe.local.response["type"] = "download"
    frappe.local.response["filename"] = file_doc.file_name
    frappe.local.response["filecontent"] = obj["Body"].read()
    frappe.local.response["content_type"] = obj.get("ContentType")
    frappe.local.response["display_content_as"] = "inline"


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
    if doc.file_url and doc.file_url.startswith(GENERATE_FILE) and is_configured():
        S3Operations().delete_from_s3(doc.content_hash)


@frappe.whitelist()
def ping():
    return "pong"
