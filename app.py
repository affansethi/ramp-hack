from flask import Flask, jsonify, render_template

import data

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/files")
def api_files():
    return jsonify(data.list_files())


if __name__ == "__main__":
    app.run(debug=True)
