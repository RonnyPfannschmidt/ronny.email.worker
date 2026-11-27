<script>
  import { onMount } from "svelte";
  import { cache, listen, oneAtATime, read } from "#lib/api.js";

  const MAIL = "/api/mail";
  let mail = $state(cache.get(MAIL) ?? null);
  let error = $state("");

  const load = oneAtATime(async () => {
    try {
      mail = await read(MAIL);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  });

  // A sync finishing is what brings folders in.
  const synced = (task) => {
    if (task.kind.startsWith("sync") && task.status === "done") load();
  };

  onMount(() => listen(load, synced));
</script>

<svelte:head><title>mail — mailmind</title></svelte:head>

<h1>Mail</h1>
{#if error}<div class="banner error">{error}</div>{/if}

{#snippet branch(nodes)}
  <ul>
    {#each nodes as node (node.label)}
      <li>
        {#if node.folder}
          <a href="/mail/{node.folder.id}">{node.label}</a>
          <span class="muted small">
            {node.folder.cached_messages}{node.folder.server_message_count !==
            node.folder.cached_messages
              ? ` of ${node.folder.server_message_count}`
              : ""}
          </span>
          {#if !node.folder.exists_on_server}<span class="warn small">proposed, not made</span>{/if}
        {:else}
          <span class="muted">{node.label}</span>
        {/if}
        {#if node.children.length}{@render branch(node.children)}{/if}
      </li>
    {/each}
  </ul>
{/snippet}

{#if !mail}
  <p class="muted">loading</p>
{:else if !mail.account}
  <p class="muted">No account yet. <a href="/accounts">Add one</a>.</p>
{:else if !mail.tree.length}
  <p class="muted">
    Nothing synced in {mail.account.name} yet — <a href="/accounts">sync it</a> and the folders
    turn up here.
  </p>
{:else}
  <p class="small muted">
    What has been synced, not what is on the server this second. Reading here changes nothing:
    nothing is marked read, and filing goes to the review queue like any other proposal.
  </p>
  <!-- The names carry the hierarchy; the tree is drawn from them. A level with no folder of
       its own is a label, because a parent can exist only inside its children's names. -->
  <div class="tree">{@render branch(mail.tree)}</div>
{/if}

<style>
  .tree :global(ul) {
    list-style: none;
    margin: 0.1rem 0;
    padding-left: 1.1rem;
  }
  .tree > :global(ul) {
    padding-left: 0;
  }
  .tree :global(li) {
    padding: 0.1rem 0 0.1rem 0.5rem;
    border-left: 1px solid var(--line);
  }
  .tree > :global(ul > li) {
    border-left: none;
    padding-left: 0;
  }
</style>
