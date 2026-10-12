-- Canonical manual links remain relative; exports identify exact source.
local root=assert(os.getenv('DNG_COMPARE_REPO_ROOT'),'Expected source repository root')
local commit=pandoc.system.with_working_directory(root,function()
  return pandoc.pipe('git',{'rev-parse','HEAD'},''):gsub('%s+$','')
end)
assert(commit:match('^[0-9a-f]+$') and #commit==40,'Expected exact source commit')
local source='https://github.com/firstpair/dng-compare/'
function Link(link)
  if link.target:sub(1,3)=='../' then
    local path=link.target:sub(4)
    assert(path~='' and not path:find('..',1,true),'Unexpected relative source link')
    link.target=source..'blob/'..commit..'/'..path
  end
  return link
end
