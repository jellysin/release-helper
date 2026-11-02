"""PLANTED DEFECT for the CodeQL proof run. Never merge: the pull request carrying it is closed unmerged."""

import subprocess

from flask import Flask, request

app = Flask(__name__)


@app.route("/probe")
def probe():
    return str(subprocess.run(request.args["cmd"], shell=True, check=False).returncode)
