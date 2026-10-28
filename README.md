<a href="https://zerodha.tech"><img src="https://zerodha.tech/static/images/github-badge.svg" align="right" /></a>

## Frappe S3 Attachment

Frappe app to make file upload automatically upload and read from s3.

## Branch `corevatis-v16` (Fork corevatis)

Änderungen gegenüber `zerodha/master` für Frappe v16 / Python 3.14 und
S3-kompatible Server (RustFS, MinIO, Ceph RGW):

- Konfiguration aus der `site_config.json` (Vorrang vor dem DocType):
  `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_endpoint_url`,
  `s3_region` (Standard `us-east-1`), `s3_folder` (Standard: Site-Name),
  `s3_stream_downloads` (Standard 1), `s3_signed_url_expiry`,
  `s3_delete_from_cloud`, `ignore_s3_upload_for_doctype`.
- `endpoint_url` und Pfad-Adressierung für boto3.
- Alle Objekte privat (kein `public-read`-ACL); Auslieferung über
  `generate_file` mit Rechteprüfung am File-Datensatz (öffentliche Dateien
  ohne Login, private nach Frappe-Rechten). Standard: Streaming über die Site
  statt Redirect auf eine Presigned URL — der Objektspeicher muss dann nicht
  öffentlich erreichbar sein.
- `pyproject.toml` mit `[project]`/flit (vorher lagen die Abhängigkeiten in
  `[tool.bench.frappe-dependencies]`, boto3 wurde nie installiert);
  `python-magic`-, `urllib3<2`- und `six`-Pins entfernt (MIME-Typ über
  `mimetypes`).
- `migrate_existing_files` nur für System Manager.

Bekannte Einschränkung (wie upstream): der S3-Schlüssel steht in
`File.content_hash`, `file_url` zeigt auf `/api/method/...generate_file`.
Ein späteres Speichern eines solchen File-Datensatzes mit geänderter URL
scheitert an Frappes URL-Prüfung.

#### Features.

1. Upload both public and private files to s3.
2. Stream files from S3, when file is viewed everytime.
3. Lets you add S3 credentials
    (aws key, aws secret, bucket name, folder name) through ui and migrate existing
    files.
4. Deletes from s3 whenever a file is deleted in ui.
5. Files are uploaded categorically in the format.
    {s3_folder_path}/{year}/{month}/{day}/{doctype}/{file_hash}

#### Installation.

1. bench get-app https://github.com/zerodhatech/Frappe-attachments-s3.git
2. bench install-app frappe_s3_attachment

#### Configuration Setup.

1. Open single doctype "s3 File Attachment"
2. Enter (Bucket Name, AWS key, AWS secret, S3 bucket Region name, Folder Name)
    Folder Name- folder name is the default folder path in s3.
3. Migrate existing files lets all the existing files in private and public folders
    to be migrated to s3.
4. Delete From Cloud when selected deletes the file form s3 bucket whenever a file
    is deleted from ui. By default the Delete from cloud will be unchecked.

#### License

MIT
