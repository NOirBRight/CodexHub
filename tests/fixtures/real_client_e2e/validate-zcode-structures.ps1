param([Parameter(Mandatory = $true)][string]$CaseRoot)

$ErrorActionPreference = 'Stop'

function Assert-ExactProperties {
    param([object]$Value, [string[]]$Names)
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if (($actual -join ',') -cne ($expected -join ',')) { throw 'schema' }
}

function Read-Json {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'missing' }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
}

try {
    $rootUrl = 'http://127.0.0.1:19190'
    $catalog = Read-Json (Join-Path $CaseRoot 'appdata\roaming\ZCode\model-providers\codexhub.json')
    $cache = Read-Json (Join-Path $CaseRoot '.zcode\v2\bots-model-cache.v2.json')
    $config = Read-Json (Join-Path $CaseRoot '.zcode\v2\config.json')
    Assert-ExactProperties $catalog @('schemaVersion', 'providers')
    Assert-ExactProperties $cache @('schemaVersion', 'providers')
    Assert-ExactProperties $config @('provider')
    if ($catalog.schemaVersion -cne 'zcode.model-providers.v2' -or
        $cache.schemaVersion -cne 'zcode.model-providers.v2' -or
        $catalog.providers -isnot [System.Array] -or $cache.providers -isnot [System.Array]) {
        throw 'collection'
    }
    $expected = [ordered]@{
        'codexhub-openai' = [pscustomobject]@{ route = 'openai'; model = 'gpt-5.6-luna' }
        'codexhub-volc' = [pscustomobject]@{ route = 'volc'; model = 'glm-5.2' }
    }
    foreach ($id in $expected.Keys) {
        $details = $expected[$id]
        $catalogProvider = @($catalog.providers | Where-Object { $_.id -ceq $id })
        $cacheProvider = @($cache.providers | Where-Object { $_.id -ceq $id })
        if ($catalogProvider.Count -ne 1 -or $cacheProvider.Count -ne 1) { throw 'provider' }
        foreach ($provider in @($catalogProvider[0], $cacheProvider[0])) {
            Assert-ExactProperties $provider @(
                'id', 'name', 'enabled', 'source', 'apiFormat', 'endpoints',
                'apiKeyRequired', 'apiKey', 'defaultKind', 'models', 'createdAt', 'updatedAt'
            )
            if ($provider.enabled -ne $true -or $provider.source -cne 'custom' -or
                $provider.apiFormat -cne 'openai-responses' -or
                $provider.apiKeyRequired -ne $true -or -not [string]$provider.apiKey -or
                $provider.defaultKind -cne 'openai' -or $provider.models -isnot [System.Array] -or
                @($provider.models).Count -ne 1) { throw 'provider-shape' }
            $model = @($provider.models)[0]
            Assert-ExactProperties $model @('id', 'name', 'kinds', 'defaultKind', 'modalities', 'maxOutputTokens')
            Assert-ExactProperties $model.modalities @('input', 'output')
            if ($model.id -cne $details.model -or $model.kinds -isnot [System.Array] -or
                @($model.kinds).Count -ne 1 -or @($model.kinds)[0] -cne 'openai' -or
                $model.defaultKind -cne 'openai' -or $model.modalities.input -isnot [System.Array] -or
                $model.modalities.output -isnot [System.Array] -or $model.maxOutputTokens -ne 32768) {
                throw 'model-array'
            }
        }
        if ($catalogProvider[0].endpoints.baseURL -cne $rootUrl -or
            $catalogProvider[0].endpoints.paths.openai -cne "/v1/providers/$($details.route)/responses") {
            throw 'catalog-endpoint'
        }
        $providerUrl = "$rootUrl/v1/providers/$($details.route)"
        if ($cacheProvider[0].endpoints.baseURL -cne $providerUrl -or
            $cacheProvider[0].endpoints.paths.openai -cne '/responses') {
            throw 'cache-endpoint'
        }
        $configProvider = $config.provider.PSObject.Properties[$id].Value
        Assert-ExactProperties $configProvider @(
            'name', 'kind', 'enabled', 'source', 'apiFormat', 'endpoints', 'options', 'models'
        )
        Assert-ExactProperties $configProvider.options @('baseURL', 'apiKey', 'apiKeyRequired')
        if ($configProvider.kind -cne 'openai' -or $configProvider.enabled -ne $true -or
            $configProvider.source -cne 'custom' -or $configProvider.apiFormat -cne 'openai-responses' -or
            $configProvider.endpoints.baseURL -cne $providerUrl -or
            $configProvider.endpoints.paths.openai -cne '/responses' -or
            $configProvider.options.baseURL -cne $providerUrl -or
            $configProvider.options.apiKeyRequired -ne $true -or -not [string]$configProvider.options.apiKey -or
            $configProvider.models -is [System.Array]) { throw 'config-provider' }
        Assert-ExactProperties $configProvider.models @($details.model)
        $configModel = $configProvider.models.PSObject.Properties[$details.model].Value
        Assert-ExactProperties $configModel @('name', 'limit', 'modalities')
        Assert-ExactProperties $configModel.limit @('output')
        Assert-ExactProperties $configModel.modalities @('input', 'output')
        if ($configModel.limit.output -ne 32768 -or
            $configModel.modalities.input -isnot [System.Array] -or
            $configModel.modalities.output -isnot [System.Array]) { throw 'config-model' }
    }
    if (@($catalog.providers).Count -ne 2 -or @($cache.providers).Count -ne 2 -or
        @($config.provider.PSObject.Properties).Count -ne 2) { throw 'provider-count' }
}
catch {
    exit 1
}
