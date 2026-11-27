import { get, post } from "./api.js";

/** The header's half of every view: the accounts, the chosen one, and what waits where. */
export const session = $state({
  accounts: [],
  current_account: null,
  waiting: {},
  // Bumped on a switch, so the view being shown is drawn again for the other account.
  switched: 0,
});

export async function refresh() {
  Object.assign(session, await get("/api/session"));
}

export async function choose(accountId) {
  Object.assign(session, await post("/api/account/choose", [["account_id", accountId]]));
  session.switched += 1;
}
