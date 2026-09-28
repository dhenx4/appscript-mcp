#!/usr/bin/env python3
"""
appscript_mcp - remote MCP server that lets Claude read and write the source
of your Google Apps Script projects, via the Apps Script API
(script.googleapis.com), authenticated as you through Google OAuth.

This server IS the OAuth layer Claude's custom-connector flow talks to. It
proxies Google's OAuth under the hood (via fastmcp's GoogleProvider), and
tool functions reuse that same Google access token to call the Apps Script
API directly - no separate service account or token wrangling needed.

Required environment variables (set these on the Cloud Run service):
  GOOGLE_CLIENT_ID       - OAuth 2.0 Web application client ID
  GOOGLE_CLIENT_SECRET   - OAuth 2.0 client secret
  BASE_URL               - Public HTTPS URL of this deployed service,
                            e.g. https://appscript-mcp-xxxxx-uc.a.run.app
                            (no trailing slash)
  JWT_SIGNING_KEY        - Any long random string, fixed across restarts
                            (generate once with: python3 -c "import secrets; print(secrets.token_urlsafe(32))")

Scopes requested: https://www.googleapis.com/auth/script.projects
(read + write access to Apps Script project source, for whichever projects
your Google account can already edit - nothing broader).
"""

import os
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token

SCRIPT_API_BASE = "https://script.googleapis.com/v1"

# ---- Auth: this server acts as an OAuth proxy in front of Google OAuth ----

# .get(...) with placeholders (not [...]) so the FIRST deploy - before you've
# created the OAuth client and therefore don't have real values yet - still
# boots and gives you a Cloud Run URL. Nobody can complete auth against the
# placeholder values, which is fine: you're only deploying once here to learn
# the URL, then filling in real env vars and redeploying (see README.md).
auth_provider = GoogleProvider(
    client_id=os.environ.get("GOOGLE_CLIENT_ID", "unconfigured.apps.googleusercontent.com"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "unconfigured"),
    base_url=os.environ.get("BASE_URL", "http://localhost:8080"),
    required_scopes=[
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/script.projects",
    ],
    jwt_signing_key=os.environ.get("JWT_SIGNING_KEY", "unconfigured-temporary-signing-key"),
)

mcp = FastMCP(name="appscript_mcp", auth=auth_provider)


# ---- Shared helpers ----------------------------------------------------

def _google_bearer_token() -> str:
    """Pulls the upstream Google access token for the currently authenticated
    request. GoogleProvider's OAuth-proxy pattern makes the real Google token
    (with whatever scopes we requested above) available here, not just an
    opaque proxy token - see fastmcp's OAuth Proxy docs, "Calling downstream
    services". We use it to call script.googleapis.com directly, which is
    the same provider (Google) and the same scope we already consented to,
    not an unrelated third-party handoff.
    """
    token = get_access_token()
    raw = getattr(token, "token", None) or getattr(token, "access_token", None)
    if not raw:
        raise RuntimeError(
            "Could not find the underlying Google access token on the "
            "current auth context. Re-authenticate the connector and retry."
        )
    return raw


async def _script_api_request(method: str, path: str, **kwargs: Any) -> dict:
    """Reusable authenticated call against the Apps Script API."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_google_bearer_token()}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method, f"{SCRIPT_API_BASE}/{path}", headers=headers, **kwargs
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Apps Script API error {response.status_code} on {method} {path}: "
            f"{response.text[:500]}"
        )
    return response.json() if response.content else {}


# ---- Models --------------------------------------------------------------

class GetProjectContentInput(BaseModel):
    """Input for fetching an Apps Script project's full source."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    script_id: str = Field(
        ...,
        description=(
            "The Apps Script project ID, i.e. the id segment of its "
            "script.google.com/d/<ID>/edit URL, or the Drive file id for a "
            "file with mimeType application/vnd.google-apps.script."
        ),
        min_length=10,
    )


class ScriptFile(BaseModel):
    """One source file within an Apps Script project."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="File name without extension, e.g. 'Code' or 'appsscript'.")
    type: Literal["SERVER_JS", "HTML", "JSON"] = Field(
        ..., description="'SERVER_JS' for .gs files, 'HTML' for .html files, 'JSON' for the appsscript manifest."
    )
    source: str = Field(..., description="Full source text of the file.")


class UpdateProjectContentInput(BaseModel):
    """Input for overwriting an Apps Script project's full source.

    The Apps Script API's updateContent call REPLACES the entire project
    with the file list given here - it is not a per-file patch. Always
    apps_script_get_content first, edit only the file(s) you need to change
    in that result, and pass the COMPLETE file list back (including the
    unchanged files and the appsscript.json manifest), or you will silently
    delete every file you omit.
    """
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    script_id: str = Field(..., description="The Apps Script project ID to overwrite.", min_length=10)
    files: list[ScriptFile] = Field(
        ...,
        description=(
            "The COMPLETE set of files the project should contain after this "
            "call, including every file returned by apps_script_get_content "
            "that you are not changing."
        ),
        min_length=1,
    )


class CreateVersionInput(BaseModel):
    """Input for snapshotting an Apps Script project's current saved content as a version."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    script_id: str = Field(..., description="The Apps Script project ID to version.", min_length=10)
    description: str = Field(
        default="", description="Optional human-readable label for this version, e.g. 'Fix empty roster resolution bug'."
    )


# ---- Tools -----------------------------------------------------------------

@mcp.tool(
    name="apps_script_get_content",
    annotations={
        "title": "Get Apps Script Project Source",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def apps_script_get_content(params: GetProjectContentInput) -> dict:
    """Fetch the full current source of a Google Apps Script project.

    Returns every file in the project (each .gs/.html file plus the
    appsscript.json manifest) with its complete source text. Use this before
    apps_script_update_content, since an update must resend every file you
    want to keep.

    Args:
        params (GetProjectContentInput): script_id - the Apps Script project ID.

    Returns:
        dict: {
            "scriptId": str,
            "files": [
                {"name": str, "type": "SERVER_JS"|"HTML"|"JSON", "source": str,
                 "functionSet": {...}, "createTime": str, "updateTime": str}
            ]
        }
        as returned by script.googleapis.com's projects.getContent.

    Error Handling:
        Raises a RuntimeError with the upstream status code and body if the
        project ID is wrong, not shared with the authenticated account, or
        the account lacks edit access.
    """
    return await _script_api_request("GET", f"projects/{params.script_id}/content")


@mcp.tool(
    name="apps_script_update_content",
    annotations={
        "title": "Update Apps Script Project Source",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def apps_script_update_content(params: UpdateProjectContentInput) -> dict:
    """Overwrite a Google Apps Script project's saved source with a new, complete file set.

    This REPLACES all source files in the project - it is not a diff/patch.
    Always call apps_script_get_content first, modify only the files you
    intend to change, and pass every other file back unmodified (including
    the appsscript.json manifest), or those files will be deleted from the
    project.

    This updates the project's saved (HEAD) source only. It does not create
    a version and does not redeploy any existing web-app deployment - call
    apps_script_create_version afterwards if you want a durable snapshot,
    and note that a deployed web app / trigger keeps running the version it
    was deployed against until a new version is deployed to it.

    Args:
        params (UpdateProjectContentInput): script_id and the complete files list.

    Returns:
        dict: {"scriptId": str, "files": [...]} - the project's new saved content,
        as echoed back by script.googleapis.com's projects.updateContent.

    Error Handling:
        Raises a RuntimeError with the upstream status code and body on
        failure (e.g. invalid JSON in appsscript.json, or missing edit access).
    """
    body = {"files": [f.model_dump() for f in params.files]}
    return await _script_api_request(
        "PUT", f"projects/{params.script_id}/content", json=body
    )


@mcp.tool(
    name="apps_script_create_version",
    annotations={
        "title": "Create Apps Script Version",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def apps_script_create_version(params: CreateVersionInput) -> dict:
    """Create an immutable version snapshot of an Apps Script project's current saved source.

    Time-driven and installable triggers always run the project's latest
    SAVED content regardless of versions, so this is optional for triggers to
    pick up a fix - but it gives you a labeled rollback point, and is
    required if you also want to point an existing web-app deployment at the
    new code via projects.deployments.update (not exposed by this server).

    Args:
        params (CreateVersionInput): script_id and an optional description.

    Returns:
        dict: {"scriptId": str, "versionNumber": int, "description": str, "createTime": str}

    Error Handling:
        Raises a RuntimeError with the upstream status code and body on failure.
    """
    body = {"description": params.description} if params.description else {}
    return await _script_api_request(
        "POST", f"projects/{params.script_id}/versions", json=body
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    mcp.run(transport="http", host="0.0.0.0", port=port)
