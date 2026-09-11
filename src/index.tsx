import { callable, definePlugin, toaster } from "@decky/api";
import { ButtonItem, PanelSection, PanelSectionRow, ToggleField, staticClasses } from "@decky/ui";
import { useEffect, useState } from "react";
import { FaPlug } from "react-icons/fa";

type Status = {
  connected: boolean;
  selected: boolean;
  gpu_name: string | null;
  bus_id: string | null;
  power_cap_w: number | null;
  auto_switch: boolean;
  reopen_game: boolean;
  pending_games: { appid: number; name: string }[];
  busy: boolean;
  error: string | null;
  message: string | null;
  conflict: string | null;
};
type Result = { ok: boolean; error?: string };
const getStatus = callable<[], Status>("get_status");
const action = callable<[action: string], Result>("run_action");
const setAutoSwitch = callable<[enabled: boolean], Result>("set_auto_switch");
const setReopenGame = callable<[enabled: boolean], Result>("set_reopen_game");

function Content() {
  const [status, setStatus] = useState<Status>();
  const [pending, setPending] = useState(false);
  const [connectionError, setConnectionError] = useState<string>();
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const value = await getStatus();
        if (alive) { setStatus(value); setConnectionError(undefined); }
      } catch (error) {
        if (alive) setConnectionError(String(error));
      } finally {
        if (alive) timer = setTimeout(refresh, 2000);
      }
    };
    void refresh();
    return () => { alive = false; clearTimeout(timer); };
  }, []);
  const busy = pending || !status || status.busy || !!connectionError;
  const perform = async (fn: () => Promise<Result>) => {
    setPending(true);
    try {
      const result = await fn();
      if (!result.ok) throw new Error(result.error || "Operation failed");
      setStatus(await getStatus());
    } catch (error) {
      toaster.toast({ title: "decky-onexgpu", body: String(error) });
    } finally { setPending(false); }
  };
  return <PanelSection>
    {status?.connected && <>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || !!status.conflict} onClick={() => void perform(() => action("eject-sleep"))}>Eject eGPU then sleep</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ToggleField label="Reopen game after resume" description="Relaunch the running Steam game after resume. Progress is not restored — save in-game first." checked={status?.reopen_game ?? false} disabled={busy || !!status?.conflict} onChange={value => void perform(() => setReopenGame(value))} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || !!status.conflict} onClick={() => void perform(() => action("eject"))}>Eject eGPU</ButtonItem></PanelSectionRow>
    </>}
    {status && (!status.connected || !status.selected) && <PanelSectionRow><ButtonItem layout="below" disabled={busy || !!status.conflict} onClick={() => void perform(() => action("switch"))}>Switch to eGPU</ButtonItem></PanelSectionRow>}
    <PanelSectionRow><div style={{ fontSize: 13, lineHeight: 1.5, padding: "8px 0", overflowWrap: "anywhere" }} aria-live="polite">
      <strong>Status</strong><br />
      {!status ? "Reading GPU status…" : status.connected ? <>
        <strong>{status.gpu_name}</strong><br />
        {status.bus_id}<br />
        {status.selected ? "Selected as primary eGPU" : "Connected · not selected as primary"}
        {status.power_cap_w != null && <><br />Driver power cap: {status.power_cap_w} W</>}
      </> : "No supported eGPU detected"}
      {status && status.reopen_game && status.pending_games.length > 0 && <><br />Will reopen: {status.pending_games.map(game => game.name).join(", ")}</>}
      {status?.busy && <><br />Working…</>}
      {(connectionError || status?.error || status?.conflict || status?.message) && <><br />{connectionError || status?.error || status?.conflict || status?.message}</>}
    </div></PanelSectionRow>
    <PanelSectionRow><ToggleField label="Auto-switch on wake" description="Select a connected eGPU after sleep. Switching restarts the graphical session." checked={status?.auto_switch ?? false} disabled={busy || !!status?.conflict} onChange={value => void perform(() => setAutoSwitch(value))} /></PanelSectionRow>
    <PanelSectionRow><ButtonItem layout="below" description="Recovery: restarts the graphical session and closes running games." disabled={busy} onClick={() => void perform(() => action("restart"))}>Restart Display Manager</ButtonItem></PanelSectionRow>
  </PanelSection>;
}

export default definePlugin(() => ({
  name: "decky-onexgpu",
  titleView: <div className={staticClasses.Title}>decky-onexgpu</div>,
  content: <Content />,
  icon: <FaPlug />,
}));
