from flask import Flask, render_template, send_from_directory

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/data/<path:filename>")
def data_file(filename):
    """data/ フォルダ内の PDF 等を配信"""
    return send_from_directory("data", filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
