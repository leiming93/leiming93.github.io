document.documentElement.classList.add('js');
const toggle = document.querySelector('.nav-toggle');
const nav = document.querySelector('#site-nav');
if (toggle && nav) {
  const close = () => {nav.classList.remove('is-open'); toggle.setAttribute('aria-expanded', 'false'); toggle.textContent = '菜单';};
  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    nav.classList.toggle('is-open', open);
    toggle.textContent = open ? '收起' : '菜单';
  });
  document.addEventListener('keydown', e => {if (e.key === 'Escape' && nav.classList.contains('is-open')) {close(); toggle.focus();}});
  nav.addEventListener('click', e => {if (e.target.closest('a')) close();});
}
