"""
CLIQ File Attachment & Ingestion Service (Phase V).

Safely receives, validates, sanitizes, and extracts text from user-uploaded
configuration and diagnostic files for AI Copilot context injection.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("cliq.ai.attachments")

# Strict set of allowed network engineering and script extensions
ALLOWED_EXTENSIONS = {
    ".txt", ".log", ".py", ".yaml", ".yml", ".json",
    ".xml", ".cfg", ".conf", ".csv", ".pdf", ".md"
}

# 10 MB per attachment limit
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

_DEFAULT_ATTACHMENT_DIR = Path.home() / ".cliq" / "attachments"


@dataclass
class AttachmentInfo:
    id: str
    filename: str
    size_bytes: int
    extension: str
    text_content: str
    line_count: int
    conversation_id: str

    def to_dict(self, include_text: bool = True) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "extension": self.extension,
            "line_count": self.line_count,
            "conversation_id": self.conversation_id,
        }
        if include_text:
            d["text_content"] = self.text_content
        return d


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent directory traversal and filesystem attacks."""
    # Strip any directory path components
    basename = os.path.basename(filename.strip().replace("\\", "/"))
    # Remove control characters, null bytes, and shell metacharacters
    clean = re.sub(r"[^\w\s\.-]", "_", basename)
    clean = re.sub(r"\s+", "_", clean)
    if not clean or clean.startswith("."):
        clean = f"attachment_{uuid.uuid4().hex[:6]}.txt"
    return clean[:120]


class AttachmentService:
    """Service for validating, storing, and extracting text from uploaded attachments."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or _DEFAULT_ATTACHMENT_DIR
        os.makedirs(self.base_dir, exist_ok=True)

    def validate_file(self, filename: str, content_bytes: bytes) -> Tuple[bool, str]:
        """Validate file size and extension."""
        ext = os.path.splitext(filename.lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            return False, f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"

        if len(content_bytes) > MAX_FILE_SIZE_BYTES:
            max_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
            return False, f"File exceeds maximum size limit of {max_mb} MB"

        return True, ""

    def extract_text(self, filename: str, content_bytes: bytes) -> str:
        """Safely extract readable text from supported file formats."""
        ext = os.path.splitext(filename.lower())[1]

        if ext == ".pdf":
            # Attempt PDF extraction via pypdf if installed
            try:
                import io
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(content_bytes))
                pages = [page.extract_text() or "" for page in reader.pages[:20]]
                return "\n".join(pages).strip()
            except Exception as ex:
                log.warning("PDF extraction failed: %s; falling back to binary scan", ex)
                return "[PDF text could not be extracted; binary format]"

        # Standard text/code/config files (try utf-8, fallback to latin-1)
        try:
            return content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content_bytes.decode("latin-1")
            except Exception as ex:
                log.warning("Text decoding failed for %s: %s", filename, ex)
                return "[Unreadable text encoding]"

    def save_attachment(
        self,
        conversation_id: str,
        filename: str,
        content_bytes: bytes,
    ) -> AttachmentInfo:
        """Validate, store, and return extracted attachment info."""
        is_valid, err = self.validate_file(filename, content_bytes)
        if not is_valid:
            raise ValueError(err)

        clean_name = sanitize_filename(filename)
        ext = os.path.splitext(clean_name.lower())[1]
        att_id = f"att-{uuid.uuid4().hex[:10]}"

        conv_dir = self.base_dir / conversation_id
        os.makedirs(conv_dir, exist_ok=True)

        target_path = conv_dir / f"{att_id}_{clean_name}"
        with open(target_path, "wb") as f:
            f.write(content_bytes)

        text = self.extract_text(clean_name, content_bytes)
        lines = text.count("\n") + 1 if text else 0

        log.info("Stored attachment %s (%s, %d bytes) for conversation %s",
                 att_id, clean_name, len(content_bytes), conversation_id)

        return AttachmentInfo(
            id=att_id,
            filename=clean_name,
            size_bytes=len(content_bytes),
            extension=ext,
            text_content=text,
            line_count=lines,
            conversation_id=conversation_id,
        )


# Global attachment service instance
_attachment_service = AttachmentService()

def get_attachment_service() -> AttachmentService:
    return _attachment_service

