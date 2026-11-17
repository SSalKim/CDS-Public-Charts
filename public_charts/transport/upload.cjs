const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const {DefaultArtifactClient} = await import('@actions/artifact');
  const [name, directory] = process.argv.slice(2);
  if (!/^chart-(control|result)-[0-9]+-[0-9]+-[0-9]{6}$/.test(name)) {
    throw new Error('Invalid artifact name');
  }
  const root = path.resolve(directory);
  const files = fs.readdirSync(root).map(file => {
    if (!/^(manifest\.json|[a-f0-9]{64}\.webp)$/.test(file)) {
      throw new Error('Unexpected artifact file');
    }
    const full = path.join(root, file);
    if (!fs.lstatSync(full).isFile()) throw new Error('Expected regular file');
    return full;
  });
  const result = await new DefaultArtifactClient().uploadArtifact(name, files, root, {
    retentionDays: 1, compressionLevel: 0
  });
  if (!Number.isSafeInteger(result.id)) throw new Error('Missing artifact ID');
  process.stdout.write(`CHART_ARTIFACT_RESPONSE ${JSON.stringify({id: result.id})}\n`);
}

main().catch(error => {
  // Library errors can contain temporary signed URLs; expose only their type.
  process.stderr.write(`Chart artifact upload failed: ${error.name}\n`);
  process.exitCode = 1;
});
