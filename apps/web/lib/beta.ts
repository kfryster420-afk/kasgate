/** localStorage key that remembers the testnet beta banner was dismissed. A UI preference, not a secret. */
export const BETA_BANNER_STORAGE_KEY = "kasgate.testnetBetaBanner.dismissed";
/** Attribute set on <html> before first paint once the banner was dismissed; CSS hides the banner while it is present. */
export const BETA_BANNER_ATTRIBUTE = "data-beta-banner-dismissed";

/** Runs inline in <head> so a dismissed banner never flashes on load. Storage errors leave the banner visible. */
export const BETA_BANNER_BOOT_SCRIPT = `try{if(window.localStorage.getItem(${JSON.stringify(BETA_BANNER_STORAGE_KEY)})==="1"){document.documentElement.setAttribute(${JSON.stringify(BETA_BANNER_ATTRIBUTE)},"")}}catch(e){}`;

export const BETA_LINKS = {
  kaswareSite: "https://www.kasware.xyz/",
  kaswareChromeStore: "https://chromewebstore.google.com/detail/kasware-wallet/hklhheigdmpoolooomdihmhlpjjdbklf",
  kaswareDocs: "https://docs.kasware.xyz/wallet",
  tn10Faucet: "https://faucet-tn10.kaspanet.io/",
  kaspaWikiTestnets: "https://wiki.kaspa.org/en/testnets",
  buyerSdk: "https://github.com/kasgate/kasgate/tree/main/packages/buyer-sdk",
  runbook: "https://github.com/kasgate/kasgate/blob/main/docs/RUNBOOK.md",
  newIssue: "https://github.com/kasgate/kasgate/issues/new/choose",
} as const;
