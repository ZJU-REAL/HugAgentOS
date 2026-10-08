"""Provision a separate application data database: python -m core.services.application_hosting_setup."""

from core.services.application_store import application_engine, initialize_store


def main():
    initialize_store(application_engine())
    print("Application database registry provisioned")


if __name__ == "__main__":
    main()
