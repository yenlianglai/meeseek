from meeseek_pack_kit import serve

from meeseek_pack_gitlab import placeholders, tools


def main() -> None:
    serve("gitlab", tools(), placeholders=placeholders)


if __name__ == "__main__":
    main()
