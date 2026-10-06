from __future__ import annotations

import os

from neo4j import GraphDatabase
from ollama import Client

def main() -> int:
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        with driver.session() as session:
            value = session.run("RETURN 1 AS ok").single()["ok"]
            assert value == 1
        print("Neo4j integration OK")
    finally:
        driver.close()

    client = Client(host=os.environ["OLLAMA_HOST"])
    client.list()
    print("Ollama integration OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
