from . import data, store


if __name__ == "__main__":
    store.initialize()
    print(data.sync_local_cache()["id"])
    print(data.seed_demo()["id"])
