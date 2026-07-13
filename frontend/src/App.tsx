import { useQuery } from '@tanstack/react-query'

async function fetchVersion(): Promise<{ service: string; version: string }> {
  const response = await fetch('http://localhost:8000/version')
  if (!response.ok) throw new Error('Backend unavailable')
  return response.json()
}

export default function App() {
  const version = useQuery({ queryKey: ['version'], queryFn: fetchVersion })
  return (
    <main>
      <p className="eyebrow">PHASE 0 FOUNDATION</p>
      <h1>Executive Command Center</h1>
      <p>Local-first platform foundation is running.</p>
      <section>
        <strong>Backend</strong>
        <span>{version.isSuccess ? `${version.data.service} ${version.data.version}` : 'Connecting…'}</span>
      </section>
    </main>
  )
}
