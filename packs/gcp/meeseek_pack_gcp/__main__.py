from meeseek_pack_kit import serve

from meeseek_pack_gcp import placeholders, tools


def main() -> None:
    serve("gcp", tools(), placeholders=placeholders)


if __name__ == "__main__":
    main()
