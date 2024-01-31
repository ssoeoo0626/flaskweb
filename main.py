from flask import Flask

app = Flask(__name__)

@app.route('/')
def index():
    return "Hello World!!"

@app.route('/hello')
def hello():
    return "안녕! 반가워!"


if __name__ == '__main__':
    app.run(debug = True)