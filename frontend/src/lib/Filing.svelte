<script>
  import { goto } from "$app/navigation";
  import { post } from "./api.js";

  /** `picked` is what the proposal is about; `them` is how the buttons name it. */
  let { containerId, folders, picked, them = "them" } = $props();
  let target = $state("");
  let named = $state("");
  let filing = $state(false);
  let problem = $state("");

  const offered = $derived(folders.filter((row) => row.folder && row.folder.id !== containerId));

  async function propose(operation) {
    filing = true;
    problem = "";
    try {
      const answer = await post(`/api/mail/${containerId}/file`, [
        ...picked.map((id) => ["message_id", id]),
        ["operation", operation],
        ["target_container_id", target],
        ["target_container_name", named],
      ]);
      if (answer.bundle_id) await goto(`/bundle/${answer.bundle_id}`);
      else problem = answer.problem;
    } catch (failed) {
      problem = failed.message;
    } finally {
      filing = false;
    }
  }
</script>

{#if problem}<div class="banner error">{problem}</div>{/if}
<div class="actions">
  <label>
    move {them} to
    <select name="target_container_id" bind:value={target}>
      <option value="">— an existing folder —</option>
      {#each offered as row (row.folder.id)}
        <option value={String(row.folder.id)}>
          {"  ".repeat(row.depth)}{row.label}{row.folder.exists_on_server ? "" : " (to be made)"}
        </option>
      {/each}
    </select>
  </label>
  <label>or a new one <input name="target_container_name" bind:value={named} placeholder="Archive/2026" /></label>
  <button disabled={filing} onclick={() => propose("move")}>propose the move</button>
  <button disabled={filing} onclick={() => propose("delete")}>propose deleting {them}</button>
</div>
<p class="small muted">
  Nothing moves here. Either button writes a proposal and opens it for review, where the whole
  effect is listed and accepting it is what touches the mailbox.
</p>
