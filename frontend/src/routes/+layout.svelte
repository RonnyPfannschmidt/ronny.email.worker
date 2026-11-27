<script>
  import { onMount } from "svelte";
  import { page } from "$app/state";
  import { listen } from "#lib/api.js";
  import { choose, refresh, session } from "#lib/session.svelte.js";

  let { children } = $props();
  let error = $state("");

  const others = (account) => {
    const waiting = session.waiting[account.id];
    return waiting && account.id !== session.current_account?.id ? ` (${waiting} waiting)` : "";
  };

  async function load() {
    try {
      await refresh();
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  }

  async function switchTo(event) {
    try {
      await choose(Number(event.currentTarget.value));
    } catch (problem) {
      error = problem.message;
    }
  }

  // What waits where changes with the queue, so the header follows it too.
  onMount(() => listen(load, null, load));
</script>

<nav class="small">
  <a href="/">review queue</a><a href="/mail">mail</a><a href="/accounts">accounts</a>
  <a href="/agents" data-sveltekit-reload>agents</a>
  {#if session.accounts.length > 1}
    <label class="switch">
      working in
      <select value={session.current_account?.id} onchange={switchTo}>
        {#each session.accounts as account (account.id)}
          <option value={account.id}>{account.name}{others(account)}</option>
        {/each}
      </select>
    </label>
  {:else if session.current_account}
    <span class="muted switch">
      working in <span class="here">{session.current_account.name}</span>
    </span>
  {/if}
</nav>
{#if error}<div class="banner error">{error}</div>{/if}

<!-- A view is drawn fresh for each address and each account; what it read before is in the
     cache, so drawing it again is instant. -->
{#key `${session.switched} ${page.url.pathname}${page.url.search}`}
  {@render children()}
{/key}

<style>
  .switch {
    margin-left: auto;
  }
</style>
