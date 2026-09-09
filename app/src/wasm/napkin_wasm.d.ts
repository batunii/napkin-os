/* tslint:disable */
/* eslint-disable */

/**
 * One browser session's Napkin: a store, and the document currently open in it.
 */
export class NapkinHost {
    free(): void;
    [Symbol.dispose](): void;
    /**
     * Compose a standalone document from the open `.clan` — bindings
     * resolved, assets inlined, scripts stripped, brand chrome. The same SDK
     * call every other shell makes; only the delivery differs, because there
     * is no headless browser here to turn it into a PDF.
     */
    composeExport(provenance: boolean, no_brand: boolean): any;
    /**
     * The packed archive of the open document — the single-file handoff, and
     * on the web the only way work leaves the browser.
     */
    download(): Uint8Array;
    entry(path: string): string;
    /**
     * The `clan://` surface, as a function call.
     *
     * `/patch-data`, `/assets/…`, `/chain`, `/apps`, `/launch` — the same
     * table the desktop reaches through a custom URI scheme and the server
     * reaches over HTTP. `/api-proxy` is the one route that does not arrive
     * here: the page holds the credentials and answers it before we are asked.
     */
    handle(path: string, query: string, body: Uint8Array): any;
    humanHtml(): string;
    /**
     * Install a template app from its packed bytes — fetched from wherever the
     * site publishes them.
     */
    installApp(bytes: Uint8Array): any;
    listApps(): any;
    listRecent(): any;
    /**
     * Instantiate a working document from an installed app and open it.
     */
    newDocument(app_id: string, title?: string | null): any;
    constructor();
    /**
     * The launcher, which is itself a CLAN app. Built here on first use — the
     * SDK needs nothing but bytes, so there is nothing to download.
     */
    openHome(): any;
    open(doc: string): any;
    setEditMode(active: boolean): void;
    setPreviewHtml(html: string): void;
    title(): string;
    /**
     * Take an uploaded `.clan` into the store and open it.
     */
    upload(bytes: Uint8Array, suggested_id: string): any;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_napkinhost_free: (a: number, b: number) => void;
    readonly napkinhost_composeExport: (a: number, b: number, c: number) => [number, number, number];
    readonly napkinhost_download: (a: number) => [number, number, number, number];
    readonly napkinhost_entry: (a: number, b: number, c: number) => [number, number, number, number];
    readonly napkinhost_handle: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => any;
    readonly napkinhost_humanHtml: (a: number) => [number, number, number, number];
    readonly napkinhost_installApp: (a: number, b: number, c: number) => [number, number, number];
    readonly napkinhost_listApps: (a: number) => any;
    readonly napkinhost_listRecent: (a: number) => any;
    readonly napkinhost_new: () => number;
    readonly napkinhost_newDocument: (a: number, b: number, c: number, d: number, e: number) => [number, number, number];
    readonly napkinhost_open: (a: number, b: number, c: number) => [number, number, number];
    readonly napkinhost_openHome: (a: number) => [number, number, number];
    readonly napkinhost_setEditMode: (a: number, b: number) => void;
    readonly napkinhost_setPreviewHtml: (a: number, b: number, c: number) => void;
    readonly napkinhost_title: (a: number) => [number, number, number, number];
    readonly napkinhost_upload: (a: number, b: number, c: number, d: number, e: number) => [number, number, number];
    readonly __wbindgen_free: (a: number, b: number, c: number) => void;
    readonly __wbindgen_exn_store: (a: number) => void;
    readonly __externref_table_alloc: () => number;
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __wbindgen_malloc: (a: number, b: number) => number;
    readonly __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
    readonly __externref_table_dealloc: (a: number) => void;
    readonly __wbindgen_start: () => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
