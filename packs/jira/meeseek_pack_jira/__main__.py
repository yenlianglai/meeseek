from meeseek_pack_kit import serve

from meeseek_pack_jira import placeholders, tools


def main() -> None:
    serve("jira", tools(), placeholders=placeholders)


if __name__ == "__main__":
    main()
