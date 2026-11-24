<script>
  import { onMount } from "svelte";
  import { get, listen, oneAtATime, post, taskIsLive } from "./api.js";
  import Task from "./Task.svelte";
  import { when } from "./when.js";

  let { csrf } = $props();
  let accounts = $state(null);
  let error = $state("");

  const load = oneAtATime(async () => {
    try {
      accounts = (await get("/api/accounts")).accounts;
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  });

  function taskChanged(task) {
    if (!["sync_account", "sync_container"].includes(task.kind)) return;
    const account = accounts?.find((each) => each.id === task.account_id);
    if (!account || !taskIsLive(task)) load();
    else if (task.kind === "sync_account") account.task = task;
  }

  async function ask(account, path) {
    try {
      account.task = await post(path, csrf);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  }

  onMount(() => listen(load, taskChanged));
</script>

<h1>Accounts</h1>
{#if error}<div class="banner error">{error}</div>{/if}
{#if !accounts}<p class="muted">loading</p>{/if}

{#each accounts ?? [] as account (account.id)}
  <h2>
    {account.name}
    <span class="small {account.health === 'ok' ? 'ok' : 'bad'}">{account.health}</span>
  </h2>
  {#if account.health_detail}<div class="banner error">{account.health_detail}</div>{/if}

  <p class="small muted">
    {#each account.capabilities as capability, index}
      {index ? " · " : ""}
      <span
        class:bad={capability.declared && capability.offered === false}
        class:muted={!capability.declared}
      >
        {capability.name}{capability.declared && capability.offered === false
          ? " (declared, not offered)"
          : ""}{capability.declared ? "" : " (offered, not declared)"}
      </span>
    {/each}
  </p>

  <p>
    <button disabled={taskIsLive(account.task)} onclick={() => ask(account, `/api/account/${account.id}/sync`)}>
      sync now
    </button>
  </p>
  {#if account.task}
    <Task task={account.task} retry={(task) => ask(account, `/api/task/${task.id}/retry`)} />
  {/if}

  <table class="small">
    <thead><tr><th>folder</th><th>cached</th><th>on server</th><th>last synced</th></tr></thead>
    <tbody>
      {#each account.containers as folder (folder.id)}
        <tr>
          <td>{folder.name}</td>
          <td>{folder.cached_messages}</td>
          <td class="muted">{folder.server_message_count}</td>
          <td class="muted">{folder.last_synced ? when(folder.last_synced) : "never"}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/each}
