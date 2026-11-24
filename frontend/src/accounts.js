import { mount } from "svelte";
import Page from "./Accounts.svelte";

const target = document.getElementById("app");
mount(Page, { target, props: { csrf: target.dataset.csrf } });
