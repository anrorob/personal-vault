import type { ReactNode } from "react";

type Props = {
  icon?: ReactNode;
  title: string;
  description?: string;
  children?: ReactNode;
};

export function EmptyState({ icon, title, description, children }: Props) {
  return (
    <div className="pv-panel p-10 md:p-16 flex flex-col items-center text-center max-w-2xl mx-auto">
      {icon && (
        <div
          className="h-14 w-14 rounded-full flex items-center justify-center mb-4"
          style={{
            border: "1px solid var(--pv-border)",
            color: "var(--pv-silver)",
          }}
        >
          {icon}
        </div>
      )}
      <h3 className="text-lg font-semibold" style={{ color: "var(--pv-silver)" }}>
        {title}
      </h3>
      {description && (
        <p className="mt-2 text-sm max-w-md" style={{ color: "var(--pv-text-dim)" }}>
          {description}
        </p>
      )}
      {children && <div className="mt-6">{children}</div>}
    </div>
  );
}
