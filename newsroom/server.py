import uvicorn

from .app import create_app


def main():
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8088, access_log=False))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
