import { Nav } from "./components/nav";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0 }}>
        <Nav />
        {children}
      </body>
    </html>
  );
}
