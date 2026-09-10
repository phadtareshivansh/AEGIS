import BriefingPanel from "@/components/BriefingPanel";
import CommandCenter from "@/components/CommandCenter";
import Hero from "@/components/Hero";
import HowItWorks from "@/components/HowItWorks";
import { ScenarioProvider } from "@/components/ScenarioProvider";

export default function Home() {
  return (
    <main className="flex flex-1 flex-col">
      <header className="fixed inset-x-0 top-0 z-50 flex items-center justify-between px-6 py-5 mix-blend-difference md:px-12">
        <span className="font-display text-lg font-extrabold tracking-tight text-foreground">
          AEGIS
        </span>
        <span className="hidden font-mono text-xs tracking-widest text-muted sm:block">
          v0.1
        </span>
      </header>

      <ScenarioProvider>
        <Hero />
        <HowItWorks />
        <CommandCenter />
        <BriefingPanel />
      </ScenarioProvider>

      <footer className="border-t border-white/10 py-12">
        <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-6 px-6 md:flex-row md:items-end md:justify-between md:px-12">
          <p className="font-display text-2xl font-extrabold tracking-tight">
            AEGIS
          </p>
          <p className="font-mono text-xs leading-relaxed tracking-wider text-muted">
            AUTONOMOUS DISASTER RESPONSE SYSTEM
            <br />
            PREDICT · ALLOCATE · NEGOTIATE · BRIEF
          </p>
        </div>
      </footer>
    </main>
  );
}