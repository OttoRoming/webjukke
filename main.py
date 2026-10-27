import rmpc
import pickle
import subprocess
from pathlib import Path
from typing import Any, Literal
from uuid import UUID
import pwd
from datetime import datetime
import asyncio
import magic
from hashlib import sha256
import urllib.parse

from quart import (
    Quart,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
    Response as QuartResponse
)

app = Quart(__name__)
client = rmpc.Rmpc()

@app.template_filter('sha256')
def sha256_filter(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return sha256(value).hexdigest()

@app.template_filter('encode_uri_component')
def encode_uri_component_filter(value):
    return urllib.parse.quote(str(value), safe="") 

@app.get("/albumart/")
async def albumart():
    art = await client.albumart()
    if art is None:
        abort(404)

    response = await make_response(art)
    mime_type = magic.from_buffer(art)
    response.mimetype = mime_type

    return response

# "Components"
@app.get("/status/")
async def status():
    status, playing = await asyncio.gather(client.status(), client.song())

    return await render_template("status.html", status=status, playing=playing)

# Redirects
@app.get("/status/prev")
async def status_prev():
    await client.prev()
    return redirect("/status", 303)
@app.get("/status/next")
async def status_next():
    await client.next()
    return redirect("/status", 303)
@app.get("/status/pause")
async def status_pause():
    await client.pause()
    return redirect("/status", 303)
@app.get("/status/unpause")
async def status_unpause():
    await client.unpause()
    return redirect("/status", 303)
@app.get("/status/play")
async def status_play():
    await client.play()
    return redirect("/status", 303)

# Pages
@app.get("/")
async def index():
    queue, playing = await asyncio.gather(client.queue(), client.song())

    return await render_template("index.html", queue=queue, playing=playing)

@app.get("/directories/", defaults={"path": ""})
@app.get("/directories/<path:path>")
async def directories(path: str):
    lsinfo = await client.lsinfo(path)

    return await render_template("directories.html", path=str(path), lsinfo=lsinfo)

@app.get("/add/<path:path>")
async def add(path: str):
    await client.add([path])
    return redirect("/", 303)

# @app.before_serving
# async def init():
#     await db.init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3090, debug=True)
