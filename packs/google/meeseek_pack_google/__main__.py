from meeseek_pack_kit import serve

from meeseek_pack_google import tools


def main() -> None:
    serve("google", tools())


if __name__ == "__main__":
    main()
