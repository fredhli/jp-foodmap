// jpfoodmap Android shell. One module: :app — a WebView around jpfoodmap.com.
//
// FAIL_ON_PROJECT_REPOS is the default AGP template's setting and is kept deliberately:
// every dependency of this build resolves from the two repositories declared here, so a
// module that quietly adds a third cannot happen without an edit to this file.
pluginManagement {
    repositories {
        google()
        mavenCentral()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "jpfoodmap"
include(":app")
