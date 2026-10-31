/// <reference types="vite/client" />
import type { FC, RefObject } from "react";
import { IconSend, Logo } from "./Icons";
import { TurnPresence } from "./Presence";

/* Who appears in the console. Without a character it is the quiet marks
   below; with the animation module in the build (src/animation/, internal,
   left out of the open-source release) it is the character that module
   provides from src/animation/character.tsx. Nothing else imports it, so the
   console builds and runs the same with the folder gone. */

export type TurnPhase = "work" | "done" | "failed";
export type SendPhase = "rest" | "launch" | "working" | "done" | "failed";
export type PageName = "context" | "inbox" | "trace" | "usage";

export interface Character {
  /** The empty chat's figure, heading and line. */
  Empty: FC;
  emptyTitle: string;
  emptyLine: string;
  /** Beside an agent's name on its answer. */
  AuthorMark: FC<{ error: boolean }>;
  /** In the running turn's row, from `since` (ms) on. */
  Turn: FC<{ phase: TurnPhase; since: number }>;
  /** The send button's face, and the class the button wears for it. */
  Send: FC<{ phase: SendPhase }>;
  sendClass: string;
  sendTitle: (streaming: boolean) => string;
  /** What flies from the send button to the running turn; it calls onLand
   *  when it gets there, and the turn row shows the turn from then. */
  Flight: FC<{ from: RefObject<HTMLButtonElement | null>; onLand: () => void }> | null;
  /** A page's companion and what it says; onBeat each time it calls out. */
  Page: FC<{ page: PageName; say: string; stand?: "rail" | "floor"; onBeat?: () => void }> | null;
}

export const NEUTRAL: Character = {
  Empty: () => <Logo className="empty-mark" />,
  emptyTitle: "Ask for something. meeseek does it.",
  emptyLine: "It works with your context and the tools your packs connect, and shows every step right here.",
  AuthorMark: () => <Logo className="author-mark" />,
  Turn: ({ phase }) => <TurnPresence phase={phase} />,
  Send: () => <IconSend />,
  sendClass: "",
  sendTitle: (streaming) => (streaming ? "Send: this turn stops and a new one takes the new message" : "Send"),
  Flight: null,
  Page: null,
};

const found = import.meta.glob<{ character: Character }>("../animation/character.tsx", { eager: true });

export const character: Character = Object.values(found)[0]?.character ?? NEUTRAL;
