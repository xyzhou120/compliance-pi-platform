"""Explicit schema initialization for a new independent application database."""
from .settings import Settings
from .store import Store


def main():
    settings = Settings.from_env()
    store = Store(database_url=settings.database_url)
    store.engine.dispose()
    print("Initial platform schema ready. Existing incompatible schema changes require a versioned migration.")


if __name__ == "__main__":
    main()
