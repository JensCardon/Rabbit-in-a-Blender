Add as submodule:

```bash
git submodule add --name DataQualityDashboard --branch main --force ssh://git@github.com/OHDSI/DataQualityDashboard.git src/riab/libs/DataQualityDashboard
```

Set to specific tag:

```bash
VERSION=v2.6.0
cd src/riab/libs/DataQualityDashboard
git checkout ${VERSION}
cd ../../../..
git add src/riab/libs/DataQualityDashboard
git commit -m "moved DataQualityDashboard submodule to ${VERSION}"
git push
```