type Props = { size?: number; className?: string };

export function PVLogo({ size = 56, className }: Props) {
  return (
    <img
      src="/assets/branding/logo-house-transparent.svg"
      alt="Personal Vault"
      width={size}
      height={size}
      className={className}
      draggable={false}
    />
  );
}
