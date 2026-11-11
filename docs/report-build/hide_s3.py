"""Keep the project's bucket out of the published text: s3://<bucket>/<prefix> -> S3_PREFIX (as in .env.s3)."""
import re

# Public buckets the docs may cite are left alone.
S3_PATH = re.compile(r"s3://(?!eodata/|us-west-2\.opendata)[a-z0-9.-]+/[^/\s`|)]+")


def hide_s3(text: str) -> str:
    return S3_PATH.sub(lambda m: "S3\\_PREFIX" if "\\_" in m.group(0) else "S3_PREFIX", text)
