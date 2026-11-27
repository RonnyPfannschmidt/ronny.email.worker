<script>
  import { onMount } from "svelte";
  import { cache, listen, oneAtATime, post, read, taskIsLive } from "#lib/api.js";
  import Task from "#lib/Task.svelte";
  import { when } from "#lib/when.js";

  const ACCOUNTS = "/api/accounts";
  let accounts = $state(cache.get(ACCOUNTS)?.accounts ?? null);
  let error = $state("");

  const load = oneAtATime(async () => {
    try {
      accounts = (await read(ACCOUNTS)).accounts;
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
      account.task = await post(path);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  }

  onMount(() => listen(load, taskChanged));
</script>

<svelte:head><title>accounts — mailmind</title></svelte:head>

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
