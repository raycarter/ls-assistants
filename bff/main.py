import uvicorn

from app.main import app, create_app  # noqa: F401


def main() -> None:
    uvicorn.run("app.main:app", host="127.0.0.1", port=3100)


if __name__ == "__main__":
    main()
