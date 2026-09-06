"""data/ 配下のPDFを Cloudflare R2 へ一括アップロードする。

使い方:
    pip install boto3 python-dotenv
    cp .env.example .env  # R2_* を設定
    python scripts/upload_to_r2.py
    python scripts/upload_to_r2.py data/3.pdf slides/3.pdf

R2 APIトークン作成: ダッシュボード -> R2 -> Manage R2 API Tokens -> Object Read & Write
必須env: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME
"""
import glob
import mimetypes
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BUCKET = os.getenv("R2_BUCKET_NAME", "")
ENDPOINT = os.getenv("R2_ENDPOINT_URL", "")
KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
SECRET = os.getenv("R2_SECRET_ACCESS_KEY", "")


def s3():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=SECRET,
    )


def upload(local_path, key):
    ctype, _ = mimetypes.guess_type(local_path)
    s3().upload_file(
        local_path,
        BUCKET,
        key,
        ExtraArgs={"ContentType": ctype or "application/pdf"},
    )
    base = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{base}/{key}" if base else f"(presignedで配信) {key}"
    print(f"OK: {local_path} -> s3://{BUCKET}/{key}  {url}")


def main(args):
    missing = [k for k, v in
               {"R2_BUCKET_NAME": BUCKET, "R2_ENDPOINT_URL": ENDPOINT,
                "R2_ACCESS_KEY_ID": KEY_ID, "R2_SECRET_ACCESS_KEY": SECRET}.items() if not v]
    if missing:
        print(f"環境変数が不足しています: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    pairs = []
    if len(args) >= 2:
        pairs.append((args[0], args[1]))
    elif len(args) == 1:
        pairs.append((args[0], os.path.basename(args[0])))
    else:
        for path in sorted(glob.glob("data/*.pdf")):
            pairs.append((path, os.path.basename(path)))
        if not pairs:
            print("data/*.pdf が見つかりません", file=sys.stderr)
            sys.exit(1)
    for local_path, key in pairs:
        upload(local_path, key.lstrip("/"))


if __name__ == "__main__":
    main(sys.argv[1:])
