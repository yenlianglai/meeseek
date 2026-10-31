from meeseek_pack_kit import serve

from meeseek_pack_github import placeholders, tools


def main() -> None:
    serve("github", tools(), placeholders=placeholders)


if __name__ == "__main__":
    main()
