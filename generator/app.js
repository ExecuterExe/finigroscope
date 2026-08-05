/* =========================================================
   Опросный лист для формирования параметров игры
   Источник вопросов и вариантов: Tablitsy_Sovmestimosti_I_Oprosnik_30_07_26.xlsx,
   лист «Опросник».

   Совместимость ответов проверяется по листу «Совместимость параметров»
   того же файла — см. compat.js и compat-data.js.

   type    — 'single' (один вариант) или 'multi' (несколько)
   min/max — сколько вариантов можно выбрать, max: 0 — без ограничения
   other   — есть вариант «Другое»: по клику по нему открывается поле
             для своего ответа, другого способа ввести текст нет
   when    — условие показа вопроса (иначе вопрос пропускается)
   ========================================================= */

const OTHER = 'Другое';

/* helpers для условий: работают только по выбранным вариантам,
   свободный текст условия не активирует (безопасное поведение по умолчанию) */
function picked(answers, id) {
    return (answers[id] && answers[id].picked) || [];
}
function has(answers, id, value) {
    return picked(answers, id).indexOf(value) !== -1;
}

const SURVEY = [
    {
        block: 'Целеполагание и целевая аудитория',
        questions: [
            {
                id: 'Q01', num: '1',
                text: 'Какая основная цель игры?',
                type: 'multi', min: 1, max: 2,
                options: ['Развлечение', 'Обучение', 'Развитие навыков', 'Командное взаимодействие',
                    'Соревнование', 'Творческое самовыражение (свобода творить в игровом процессе)',
                    'Релаксация', 'Знакомство с новыми людьми', OTHER],
                other: true,
                placeholder: 'Например: профориентация подростков'
            },
            {
                id: 'Q02', num: '2',
                text: 'Какой возраст у целевой аудитории игры?',
                type: 'single', min: 1, max: 1,
                options: ['3-5 лет', '6-9 лет', '9-12 лет', '12-18 лет', '18-35 лет',
                    '36-50 лет', '50+ лет', 'Для всей семьи', OTHER],
                other: true,
                placeholder: 'Например: 7-10 лет или «от 14»'
            },
            {
                id: 'Q03', num: '3',
                text: 'На какой диапазон количества игроков рассчитана игра?',
                hint: 'Выберите максимально широкий диапазон, который игра должна поддерживать.',
                type: 'single', min: 1, max: 1,
                options: ['1 игрок', '2 игрока', '2-4 игрока', '2-6 игроков', '2-8 игроков',
                    '2-12 игроков', '10+ игроков (для больших компаний)', OTHER],
                other: true,
                placeholder: 'Например: 3-5'
            },
            {
                id: 'Q04', num: '4',
                text: 'Предполагается ли адаптация для лиц с ОВЗ?',
                type: 'single', min: 1, max: 1,
                options: ['Да', 'Нет'],
                other: false
            },
            {
                id: 'Q04A', num: '4.1',
                text: 'Какие виды ОВЗ нужно учесть?',
                type: 'multi', min: 1, max: 0,
                options: ['Нарушение слуха', 'Нарушение зрения', 'Речевые нарушения',
                    'Нарушения опорно-двигательного аппарата (ОДА)',
                    'ЗПР (задержка психического развития)', 'Умственная отсталость',
                    'РАС (расстройство аутистического спектра)', 'Множественные нарушения', OTHER],
                other: true,
                placeholder: 'Например: дислексия, СДВГ',
                when: function(a) { return has(a, 'Q04', 'Да'); }
            },
            {
                id: 'Q05', num: '5',
                text: 'Какое среднее время одной партии?',
                type: 'single', min: 1, max: 1,
                options: ['До 15 минут', '15-30 минут', '30-60 минут', '1-2 часа', 'Более 2 часов', OTHER],
                other: true,
                placeholder: 'Например: около 45 минут'
            },
            {
                id: 'Q06', num: '6',
                text: 'Где будет проходить игра?',
                type: 'multi', min: 1, max: 2,
                options: ['Дом', 'Дорога/в пути', 'Компания друзей', 'Игровой клуб/кафе',
                    'На открытом воздухе', OTHER],
                other: true,
                placeholder: 'Например: школьный лагерь'
            }
        ]
    },
    {
        block: 'Жанр и основные механики',
        questions: [
            {
                id: 'Q07', num: '7',
                text: 'Какой жанр предпочтителен?',
                type: 'multi', min: 1, max: 2,
                options: ['Викторина', 'Стратегия', 'Кооператив', 'Бродилка', 'Экономика / торговля',
                    'Приключение', 'Детектив / расследование', 'Карточная игра',
                    'Игра со словами и ассоциациями', 'Игра на реакцию или память',
                    'Игра с блефом и маскировкой (обман, скрытие своих намерений)',
                    'Строительство и развитие (базы, города)', OTHER],
                other: true,
                placeholder: 'Например: гонки, социальная дедукция'
            },
            {
                id: 'Q08', num: '8',
                text: 'Какой основной ресурс будут тратить и получать игроки?',
                hint: '«Время» здесь — игровой ресурс (таймер хода), а не длительность партии.',
                type: 'multi', min: 1, max: 3,
                options: ['Очки', 'Время', 'Карты', 'Деньги (игровые)', 'Предметы', 'Ходы',
                    'Здоровье/жизни', 'Опыт', 'Ресурсы (дерево, камень и т.п.)', 'Информация',
                    'Влияние/репутация', OTHER],
                other: true,
                placeholder: 'Например: энергия, топливо'
            },
            {
                id: 'Q09', num: '9',
                text: 'От чего зависит победа?',
                type: 'multi', min: 1, max: 2,
                options: ['Случайность / удача', 'Стратегическое планирование', 'Реакция/скорость',
                    'Запоминание', 'Аукцион/торги', 'Блеф/маскировка', 'Навык убеждения',
                    'Эффективность (оптимизация действий)', 'Творчество', OTHER],
                other: true,
                placeholder: 'Например: дедукция, переговоры'
            },
            {
                id: 'Q10', num: '10',
                text: 'Насколько сильно случайность влияет на игру?',
                hint: 'Случайность — это кости, перемешанные карты, рулетка.',
                type: 'single', min: 1, max: 1,
                options: ['Есть случайность и она существенно влияет на исход',
                    'Случайность есть, но влияет ограниченно (её можно компенсировать решениями)',
                    'Нет случайности (полная детерминированность)'],
                other: false
            }
        ]
    },
    {
        block: 'Взаимодействие игроков',
        questions: [
            {
                id: 'Q11', num: '11',
                text: 'Как игроки взаимодействуют друг с другом?',
                type: 'single', min: 1, max: 1,
                options: ['Прямое вредительство (можно атаковать, мешать ходам, забирать ресурсы)',
                    'Только косвенное (кто быстрее, кто больше наберет очков, без прямого вмешательства)',
                    'Кооперативное (игроки действуют как одна команда)',
                    'Смешанное (есть элементы кооперации и конкуренции)'],
                other: false,
                when: function(a) { return !has(a, 'Q03', '1 игрок'); }
            }
        ]
    },
    {
        block: 'Сюжет и сеттинг',
        questions: [
            {
                id: 'Q12', num: '12',
                text: 'Нужен ли игре сюжет?',
                type: 'single', min: 1, max: 1,
                options: ['Нужен полноценный сюжет (с историей и персонажами)',
                    'Достаточно простого антуража/тематики (без глубокой истории)',
                    'Не требуется — абстрактная игра (без сюжета и сеттинга)'],
                other: false
            },
            {
                id: 'Q13', num: '13',
                text: 'В каком мире (сеттинге) происходит игра?',
                type: 'multi', min: 1, max: 2,
                options: ['Фэнтези', 'Наука/фантастика', 'История/историческая реконструкция',
                    'Детектив', 'Бизнес', 'Постапокалипсис', 'Ужасы/хоррор',
                    'Повседневность/реализм', 'Сказочный', 'Космос', OTHER],
                other: true,
                placeholder: 'Например: киберпанк, школа магии',
                when: function(a) {
                    return !has(a, 'Q12', 'Не требуется — абстрактная игра (без сюжета и сеттинга)');
                }
            }
        ]
    },
    {
        block: 'Артефакты и компоненты',
        questions: [
            {
                id: 'Q14', num: '14',
                text: 'Требуется ли письменное заполнение (карандаш и бумага)?',
                type: 'single', min: 1, max: 1,
                options: ['Да', 'Нет'],
                other: false
            },
            {
                id: 'Q15', num: '15',
                text: 'Какие игровые предметы можно использовать?',
                hint: 'Если в вопросе 14 выбрано «Да», бумага и карандаш добавляются автоматически.',
                type: 'multi', min: 1, max: 0,
                options: ['Карты', 'Игровое поле', 'Кубики (d6, d10, d20 и др.)', 'Фишки (для игроков)',
                    'Жетоны (для ресурсов/очков)', 'Песочные часы/таймер',
                    'Фигурки/миниатюры (для самой игры)', 'Телефоны/смартфоны игроков', OTHER],
                other: true,
                placeholder: 'Например: верёвка, повязка на глаза'
            }
        ]
    },
    {
        block: 'Играбельность и баланс',
        questions: [
            {
                id: 'Q16', num: '16',
                text: 'Какая сложность игры?',
                type: 'single', min: 1, max: 1,
                options: ['Низкая (для новичков и детей)', 'Средняя (для подготовленных игроков)',
                    'Высокая (для опытных стратегов)'],
                other: false
            },
            {
                id: 'Q17', num: '17',
                text: 'Допустимо ли выбывание игрока до конца партии?',
                type: 'single', min: 1, max: 1,
                options: ['Недопустимо (игроки не выбывают до самого конца)',
                    'Допустимо (игрок может выбыть из игры до финала, то есть выбыл = проиграл)',
                    'Допустимо с возможностью возвращения (то есть выбыл ≠ проиграл, игрок может вернуться до конца игры)'],
                other: false
            },
            {
                id: 'Q18', num: '18',
                text: 'Должна ли игра помогать отстающим игрокам и притормаживать лидера?',
                hint: 'Чтобы всем было интересно до самого конца партии.',
                type: 'single', min: 1, max: 1,
                options: ['Да', 'Нет'],
                other: false,
                when: function(a) {
                    return !has(a, 'Q03', '1 игрок') &&
                        !has(a, 'Q11', 'Кооперативное (игроки действуют как одна команда)');
                }
            }
        ]
    }
];

const MAX_TEXT = 200;

/* ---------- плоский список вопросов ---------- */
const flat = [];
SURVEY.forEach(function(block) {
    block.questions.forEach(function(q) {
        q.block = block.block;
        flat.push(q);
    });
});
const byId = {};
flat.forEach(function(q) { byId[q.id] = q; });

/* Если формулировки вопросов разошлись с таблицей совместимости, проверка
   по этим вариантам молча перестала бы работать — сообщаем об этом сразу. */
(function() {
    const problems = Compat.selfTest(flat, OTHER);
    if (problems.length) {
        console.error('Опросник разошёлся с таблицей совместимости (' +
            Compat.source + '):\n' + problems.join('\n'));
    }
})();

/* ---------- состояние ---------- */
// версия в ключе: при смене формулировок вариантов старые ответы не подходят
const STORAGE_KEY = 'gg_survey_v4';
const STEP_INTRO = 0;
const STEP_BASE = 500;
const STEP_OUTRO = 900;

let currentStep;
let answers;
let animating = false;

/* Откуда пользователь перешёл по кнопке «перейти к вопросу»: экран итогов
   или другой вопрос. Пока не null — на экране есть кнопка вернуться обратно.
   Перезаписывается при каждом новом переходе, обнуляется по возвращении. */
let returnTo = null;

/* Сохранение текущего ответа перед уходом с экрана. Ставит renderQuestion,
   сбрасывают заставка и итог. Нужно, чтобы переход к другому вопросу из любой
   точки не терял правки. */
let commitCurrent = null;

const screen = document.getElementById('screen');
const progressFill = document.getElementById('progressFill');
const progressText = document.getElementById('progressText');
const restartMain = document.getElementById('restartMain');
const navMain = document.getElementById('navMain');
const navPanel = document.getElementById('navPanel');
const navList = document.getElementById('navList');
const linkFinigroskop = document.getElementById('linkFinigroskop');

function esc(s) {
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ---------- видимость вопросов ---------- */
function isVisible(q) {
    return typeof q.when !== 'function' || q.when(answers);
}

function visibleQuestions() {
    return flat.filter(isVisible);
}

/* Приводим ответы в согласованное состояние. Вызывается после каждого изменения:
   ответ на предыдущий вопрос мог скрыть вопрос целиком. */
function prune() {
    flat.forEach(function(q) {
        if (!isVisible(q)) delete answers[q.id];
    });
}

/* итоговый список ответов: «Другое» заменяется свободным текстом */
function answerList(id) {
    const a = answers[id];
    if (!a) return [];
    const list = (a.picked || []).filter(function(v) { return v !== OTHER; });
    if (a.text) list.push(a.text);
    return list;
}

function isAnswered(id) {
    return answerList(id).length > 0;
}

/* ---------- совместимость ответов ----------
   Сверяем каждый выбранный вариант с каждым другим по таблице совместимости.
   Свободный текст и «Другое» не проверяются: в таблице для них есть только
   строка «Другое», совместимая со всем «с оговорками», — она дала бы
   замечание к любому ответу и ничего бы не значила. */

/* override — {id, picked} для вопроса, который сейчас редактируется:
   его ответ ещё не сохранён, а показать противоречие нужно сразу. */
function checkCompat(override) {
    const list = [];
    visibleQuestions().forEach(function(q) {
        const picked = (override && override.id === q.id)
            ? override.picked
            : (answers[q.id] || {}).picked;
        if (!picked || !picked.length) return;
        const options = picked.filter(function(v) { return v !== OTHER; });
        if (!options.length) return;
        list.push({ id: q.id, num: q.num, text: q.text, options: options });
    });
    return Compat.check(list);
}

/* Одна пара «ответ × ответ» в списке противоречий.
   side — какая сторона пары относится к текущему вопросу: её показываем
   первой, чтобы читалось как «выбранное здесь ↔ выбранное там». */
function conflictItemHtml(conflict, forId) {
    const here = forId ? Compat.own(conflict, forId) : conflict.a;
    const there = forId ? Compat.counterpart(conflict, forId) : conflict.b;
    const jump = there.id !== here.id;
    return '<li class="conflict">' +
        '<span class="conflict-pair">' +
            '<b>' + esc(here.option) + '</b>' +
            '<span class="conflict-x">↔</span>' +
            '<b>' + esc(there.option) + '</b>' +
        '</span>' +
        '<span class="conflict-src">' +
            (jump
                ? 'вопрос ' + esc(there.num) + ' — ' + esc(there.text)
                : 'оба варианта выбраны в этом вопросе') +
        '</span>' +
        (jump
            ? '<button class="conflict-jump" type="button" data-goto="' + esc(there.id) + '">' +
                  'Перейти к вопросу ' + esc(there.num) +
                  '<span class="conflict-arrow">&#8594;</span>' +
              '</button>'
            : '') +
        '</li>';
}

function conflictBlockHtml(conflicts, level, forId) {
    if (!conflicts.length) return '';
    const hard = level === Compat.HARD;
    return '<div class="compat ' + (hard ? 'compat-hard' : 'compat-soft') + '">' +
        '<div class="compat-title">' +
            (hard
                ? 'Несовместимые ответы (' + conflicts.length + ')'
                : 'Спорные сочетания (' + conflicts.length + ')') +
        '</div>' +
        '<p class="compat-note">' +
            (hard
                ? 'По таблице совместимости такие ответы вместе не работают — стоит изменить один из них.'
                : 'Так делать можно, но сочетание нетипичное — проверьте, что вы этого хотите.') +
        '</p>' +
        '<ul class="conflict-list">' +
            conflicts.map(function(c) { return conflictItemHtml(c, forId); }).join('') +
        '</ul>' +
        '</div>';
}

/* Единственная точка перехода к произвольному вопросу.
   Сначала сохраняем то, что человек успел наменять на текущем экране, —
   иначе правки терялись бы при уходе мимо кнопок «Далее» и «Назад». */
function jumpTo(qid, remember) {
    const at = flat.indexOf(byId[qid]);
    if (at === -1) return;
    if (commitCurrent) commitCurrent();
    if (remember && currentStep !== STEP_BASE + at) returnTo = currentStep;
    go(STEP_BASE + at);
}

/* Переход к вопросу, с которым найдено противоречие. Запоминаем, откуда
   ушли, чтобы с того вопроса можно было вернуться одной кнопкой. */
function bindConflictJumps(root) {
    root.querySelectorAll('.conflict-jump').forEach(function(btn) {
        btn.onclick = function() {
            jumpTo(btn.getAttribute('data-goto'), true);
        };
    });
}

/* Подпись кнопки возврата: «к результатам» или «к вопросу N». */
function returnLabel() {
    if (returnTo === STEP_OUTRO) return 'Вернуться к результатам';
    const q = flat[returnTo - STEP_BASE];
    return q ? 'Вернуться к вопросу ' + q.num : null;
}

/* ---------- панель «все вопросы» ---------- */

function openNav() {
    const list = visibleQuestions();
    const currentQ = (currentStep >= STEP_BASE && currentStep < STEP_OUTRO)
        ? flat[currentStep - STEP_BASE]
        : null;

    navList.innerHTML = list.map(function(q, i) {
        const vals = answerList(q.id);
        const here = q === currentQ;
        return '<button class="nav-item' + (here ? ' current' : '') +
                   (vals.length ? ' done' : '') + '" type="button" ' +
                   'data-goto="' + esc(q.id) + '" ' +
                   'style="animation-delay:' + (0.02 * i) + 's">' +
            '<span class="nav-num">' + esc(q.num) + '</span>' +
            '<span class="nav-body">' +
                '<span class="nav-q">' + esc(q.text) + '</span>' +
                '<span class="nav-a">' +
                    (vals.length ? esc(vals.join(', ')) : 'не отвечено') +
                '</span>' +
            '</span>' +
            (here ? '<span class="nav-here">вы здесь</span>' : '') +
        '</button>';
    }).join('') +
    '<button class="nav-item nav-item-result" type="button" data-goto="__outro">' +
        '<span class="nav-num">&#9679;</span>' +
        '<span class="nav-body"><span class="nav-q">Результаты и генерация</span>' +
        '<span class="nav-a">итоги опроса, проверка совместимости, механики</span></span>' +
    '</button>';

    navList.querySelectorAll('.nav-item').forEach(function(btn) {
        btn.onclick = function() {
            const target = btn.getAttribute('data-goto');
            closeNav();
            if (target === '__outro') {
                if (commitCurrent) commitCurrent();
                go(STEP_OUTRO);
                return;
            }
            jumpTo(target, true);
        };
    });

    navPanel.hidden = false;
    document.body.classList.add('nav-open');
}

function closeNav() {
    navPanel.hidden = true;
    document.body.classList.remove('nav-open');
}

navMain.onclick = function() {
    if (navPanel.hidden) openNav(); else closeNav();
};
document.getElementById('navClose').onclick = closeNav;
document.getElementById('navBackdrop').onclick = closeNav;
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !navPanel.hidden) closeNav();
});

/* Ответы, которые остались непроверенными: свой текст вместо варианта из
   списка и варианты, которых в таблице нет. */
function uncheckedAnswers() {
    const list = [];
    visibleQuestions().forEach(function(q) {
        const state = answers[q.id];
        if (!state) return;
        const own = [];
        if (state.text) own.push(state.text);
        state.picked.forEach(function(option) {
            if (option !== OTHER && !Compat.isChecked(q.id, option)) own.push(option);
        });
        if (own.length) list.push({ num: q.num, text: q.text, values: own });
    });
    return list;
}

/* ---------- хранение ---------- */
function loadState() {
    answers = {};
    currentStep = STEP_INTRO;
    let saved = null;
    try {
        saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null');
    } catch (e) {
        saved = null;
    }
    if (!saved || typeof saved.currentStep !== 'number' || !saved.answers) return;

    // принимаем только знакомые вопросы и варианты — иначе старое или битое
    // состояние сломало бы рендер
    Object.keys(saved.answers).forEach(function(id) {
        const q = byId[id];
        const a = saved.answers[id];
        if (!q || !a) return;
        const pickedList = Array.isArray(a.picked) ? a.picked.filter(function(v) {
            return q.options.indexOf(v) !== -1;
        }) : [];
        // свой ответ существует только вместе с «Другое»
        const text = (typeof a.text === 'string' && pickedList.indexOf(OTHER) !== -1)
            ? a.text.slice(0, MAX_TEXT)
            : '';
        if (pickedList.length) answers[id] = { picked: pickedList, text: text };
    });
    prune();

    if (saved.currentStep === STEP_INTRO || saved.currentStep === STEP_OUTRO) {
        currentStep = saved.currentStep;
    } else {
        const idx = saved.currentStep - STEP_BASE;
        currentStep = (idx >= 0 && idx < flat.length && isVisible(flat[idx]))
            ? saved.currentStep
            : STEP_INTRO;
    }
}

function saveState() {
    try {
        sessionStorage.setItem(STORAGE_KEY,
            JSON.stringify({ answers: answers, currentStep: currentStep }));
    } catch (e) {}
}

function resetState() {
    try { sessionStorage.removeItem(STORAGE_KEY); } catch (e) {}
    answers = {};
    currentStep = STEP_INTRO;
    returnTo = null;
}

/* ---------- анимация смены шага ---------- */
let pendingRender = null;

function transition(renderFn) {
    // переход во время анимации не отбрасываем, а ставим в очередь: иначе
    // currentStep уже сменился, а на экране остался бы прежний вопрос
    if (animating) { pendingRender = renderFn; return; }
    animating = true;
    screen.classList.add('leaving');
    screen.classList.remove('entering');

    setTimeout(function() {
        renderFn();
        screen.classList.remove('leaving');
        screen.classList.add('entering');
        setTimeout(function() {
            screen.classList.remove('entering');
            animating = false;
            if (pendingRender) {
                const fn = pendingRender;
                pendingRender = null;
                transition(fn);
            }
        }, 520);
    }, 280);
}

/* ---------- прогресс ---------- */
function updateProgress() {
    const list = visibleQuestions();
    const total = list.length;
    const done = list.filter(function(q) { return isAnswered(q.id); }).length;

    if (currentStep === STEP_OUTRO) {
        progressFill.style.width = '100%';
        progressText.textContent = 'Готово';
        return;
    }
    progressFill.style.width = Math.round((done / total) * 100) + '%';

    if (currentStep === STEP_INTRO) {
        progressText.textContent = '0 из ' + total;
    } else {
        const pos = list.indexOf(flat[currentStep - STEP_BASE]);
        progressText.textContent = 'Вопрос ' + (pos + 1) + ' из ' + total;
    }
}

/* ---------- заставка ---------- */
function renderIntro() {
    commitCurrent = null;
    const total = visibleQuestions().length;
    screen.innerHTML =
        '<div class="fade-item stagger-1">' +
            '<span class="badge">Опросный лист</span>' +
            '<h1 class="title">Генератор <span class="grad">игр</span></h1>' +
            '<p class="subtitle">Ответьте на ' + total + ' вопросов — по итогам мы сформируем ' +
                'параметры вашей будущей игры. На каждом шаге можно выбрать готовый вариант ' +
                'или написать свой ответ. Часть вопросов появится или исчезнет в зависимости ' +
                'от ваших ответов, поэтому их число может измениться.</p>' +
            '<button class="btn" id="startBtn">Начать</button>' +
        '</div>';
    document.getElementById('startBtn').onclick = function() { go(firstStep()); };
    updateProgress();
}

/* ---------- вопрос ---------- */
function renderQuestion(idx) {
    const q = flat[idx];
    const state = answers[q.id] || { picked: [], text: '' };

    const list = visibleQuestions();
    const pos = list.indexOf(q);
    const prev = pos > 0 ? list[pos - 1] : null;
    const showBlockLabel = !prev || prev.block !== q.block;

    const limitText = q.type === 'multi'
        ? (q.max ? 'Можно выбрать до ' + q.max + ' вариантов' : 'Можно выбрать несколько вариантов')
        : 'Один вариант';

    let optionsHtml = '';
    q.options.forEach(function(opt, i) {
        const cls = 'option' + (q.type === 'multi' ? ' multi' : '') +
            (state.picked.indexOf(opt) !== -1 ? ' selected' : '');
        optionsHtml +=
            '<button class="' + cls + '" style="transition-delay:' + (0.03 * i) + 's" ' +
                    'data-opt="' + esc(opt) + '" type="button">' +
                '<span class="option-dot"></span>' +
                '<span class="option-text">' + esc(opt) + '</span>' +
            '</button>';
    });

    // поле для своего ответа принадлежит варианту «Другое» и живёт вместе с ним
    const otherPicked = state.picked.indexOf(OTHER) !== -1;

    // пришли сюда по кнопке из другого места — предлагаем вернуться туда же
    const showReturn = returnTo !== null && returnTo !== currentStep && returnLabel();

    screen.innerHTML =
        (showBlockLabel ? '<div class="block-label fade-item stagger-1">' + esc(q.block) + '</div>' : '') +
        '<h2 class="question fade-item stagger-2">' +
            '<span class="q-num">' + esc(q.num) + '.</span> ' + esc(q.text) +
        '</h2>' +
        (q.hint ? '<p class="q-hint fade-item stagger-2">' + esc(q.hint) + '</p>' : '') +
        '<div class="limit-line fade-item stagger-3">' + esc(limitText) + '</div>' +
        '<div class="options-wrap">' +
            '<div class="options">' + optionsHtml + '</div>' +
        '</div>' +
        (q.other
            ? '<form class="answer-form' + (otherPicked ? '' : ' hidden') + '" id="answerForm">' +
                  '<div class="input-wrap">' +
                      '<input type="text" id="answerInput" autocomplete="off" spellcheck="false" ' +
                             'maxlength="' + MAX_TEXT + '" ' +
                             'placeholder="' + esc(q.placeholder || 'Свой ответ') + '" ' +
                             'value="' + esc(state.text) + '">' +
                      '<button type="submit" class="btn send">Далее</button>' +
                  '</div>' +
              '</form>'
            : '') +
        '<div class="error-slot" id="errorSlot"></div>' +
        '<div class="saved-slot" id="savedSlot"></div>' +
        '<div class="compat-slot" id="compatSlot"></div>' +
        '<div class="nav fade-item stagger-5">' +
            (pos > 0 ? '<button class="btn ghost" id="backBtn" type="button">Назад</button>' : '') +
            (q.type === 'multi'
                ? '<button class="btn" id="nextBtn" type="button">Далее</button>'
                : '') +
            (showReturn
                ? '<button class="btn return" id="returnBtn" type="button">' +
                      '<span class="return-arrow">&#8617;</span> ' + esc(returnLabel()) +
                  '</button>'
                : '') +
        '</div>';

    const input = document.getElementById('answerInput');
    const answerForm = document.getElementById('answerForm');
    const errorSlot = document.getElementById('errorSlot');
    const savedSlot = document.getElementById('savedSlot');
    const compatSlot = document.getElementById('compatSlot');

    /* пользователь уже видел противоречие и решил оставить ответ как есть */
    let hardAccepted = false;

    if (input && otherPicked) setTimeout(function() { input.focus(); }, 400);

    /* Поле показывается вместе с выбором «Другое» и прячется вместе с ним. */
    function toggleOtherField(show) {
        if (!answerForm) return;
        answerForm.classList.toggle('hidden', !show);
        if (show) input.focus();
        else input.value = '';
    }

    function showError(msg) {
        errorSlot.innerHTML = '<div class="error">' + esc(msg) + '</div>';
        if (input) {
            input.classList.remove('shake');
            void input.offsetWidth;
            input.classList.add('shake');
        }
    }
    function clearError() { errorSlot.innerHTML = ''; }

    /* Свой ответ существует только вместе с «Другое»: если вариант снят,
       текст тоже не считается ответом. */
    function readState() {
        const picked = state.picked.slice();
        const hasOther = picked.indexOf(OTHER) !== -1;
        return {
            picked: picked,
            text: (input && hasOther)
                ? input.value.trim().replace(/\s+/g, ' ').slice(0, MAX_TEXT)
                : ''
        };
    }

    /* Сколько ответов дано. «Другое» — такой же ответ, как остальные, и
       занимает место в лимите сразу при выборе, а не после ввода текста. */
    function countable(s) {
        return s.picked.length;
    }

    function commit(s) {
        answers[q.id] = s;
        prune();
        saveState();
    }

    /* Ответ сохраняется сразу при каждом изменении, а не только по «Далее».
       Поэтому уйти к другому вопросу можно как угодно — правки не потеряются.
       Подпись нужна, чтобы это было видно, а не только работало. */
    function autoSave() {
        commit(readState());
        savedSlot.innerHTML = '<span class="saved">Ответ сохранён</span>';
    }

    /* Вызывается перед уходом с экрана из любой точки программы. */
    function keepAnswer() {
        if (input) state.text = input.value;
        answers[q.id] = readState();
        if (!isAnswered(q.id) && !answers[q.id].picked.length) delete answers[q.id];
        saveState();
    }
    commitCurrent = keepAnswer;

    /* Совместимость текущих вариантов со всеми уже данными ответами.
       Рисуем сразу при выборе — чтобы противоречие было видно на том же
       экране, где его можно исправить. Возвращает найденные пары. */
    function refreshCompat() {
        const found = Compat.involving(
            checkCompat({ id: q.id, picked: state.picked }), q.id);

        compatSlot.innerHTML =
            conflictBlockHtml(found.hard, Compat.HARD, q.id) +
            conflictBlockHtml(found.soft, Compat.SOFT, q.id);
        bindConflictJumps(compatSlot);

        if (!found.hard.length) hardAccepted = false;
        return found;
    }

    /* Противоречие не запрещает ответ, а требует осознанного решения:
       либо исправить, либо явно оставить как есть. */
    function askToKeep(proceed) {
        const block = compatSlot.querySelector('.compat-hard');
        if (!block) { proceed(); return; }

        const row = document.createElement('div');
        row.className = 'compat-actions';
        row.innerHTML = '<button class="btn small" type="button">Оставить как есть</button>' +
            '<span class="compat-hint">или измените ответ здесь либо в связанном вопросе</span>';
        row.querySelector('button').onclick = function() {
            hardAccepted = true;
            proceed();
        };
        block.appendChild(row);
        block.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    function validate(s) {
        if (s.picked.indexOf(OTHER) !== -1 && !s.text) {
            return 'Вы выбрали «Другое» — опишите свой вариант в поле выше.';
        }
        if (countable(s) < q.min) {
            return q.type === 'multi'
                ? 'Выберите хотя бы один вариант.'
                : 'Выберите вариант.';
        }
        if (q.max && countable(s) > q.max) {
            return 'Можно выбрать не больше ' + q.max + ' вариантов.';
        }
        return null;
    }

    function submit() {
        const s = readState();
        const err = validate(s);
        if (err) { showError(err); return; }

        const found = refreshCompat();
        if (found.hard.length && !hardAccepted) {
            askToKeep(function() { commit(s); goNext(idx); });
            return;
        }
        commit(s);
        goNext(idx);
    }

    if (answerForm) {
        answerForm.onsubmit = function(e) {
            e.preventDefault();
            submit();
        };
        let typing = null;
        input.oninput = function() {
            clearError();
            state.text = input.value;
            // не пишем в хранилище на каждую букву
            clearTimeout(typing);
            typing = setTimeout(autoSave, 400);
        };
    }

    screen.querySelectorAll('.option').forEach(function(btn) {
        btn.onclick = function() {
            const opt = btn.getAttribute('data-opt');
            clearError();
            // выбор изменился — прежнее «оставить как есть» больше не в счёт
            hardAccepted = false;

            if (q.type === 'single') {
                state.picked = [opt];
                screen.querySelectorAll('.option').forEach(function(b) {
                    b.classList.toggle('selected', b === btn);
                });
                toggleOtherField(opt === OTHER);

                if (opt === OTHER) {
                    // «Другое» не отвечает само по себе — ждём текст в поле
                    compatSlot.innerHTML = '';
                    return;
                }
                commit({ picked: [opt], text: '' });

                // с противоречием не уводим с экрана: сначала показываем его
                const found = refreshCompat();
                if (found.hard.length && !hardAccepted) {
                    askToKeep(function() { goNext(idx); });
                    return;
                }
                btn.classList.add('picked');
                setTimeout(function() { goNext(idx); }, 320);
                return;
            }

            // multi
            const at = state.picked.indexOf(opt);
            if (at !== -1) {
                state.picked.splice(at, 1);
                btn.classList.remove('selected');
                if (opt === OTHER) toggleOtherField(false);
            } else {
                const probe = { picked: state.picked.concat([opt]) };
                if (q.max && countable(probe) > q.max) {
                    showError('Можно выбрать не больше ' + q.max + ' вариантов. Снимите лишний.');
                    return;
                }
                state.picked.push(opt);
                btn.classList.add('selected');
                if (opt === OTHER) toggleOtherField(true);
            }
            autoSave();
            refreshCompat();
        };
    });

    const nextBtn = document.getElementById('nextBtn');
    if (nextBtn) nextBtn.onclick = submit;

    const backBtn = document.getElementById('backBtn');
    if (backBtn) backBtn.onclick = function() {
        keepAnswer();
        goPrev(idx);
    };

    const returnBtn = document.getElementById('returnBtn');
    if (returnBtn) returnBtn.onclick = function() {
        keepAnswer();
        const target = returnTo;
        returnTo = null;
        go(target);
    };

    // ответ мог быть дан раньше — показываем противоречия сразу при возврате
    if (state.picked.length) refreshCompat();

    updateProgress();
}

/* ---------- итог ---------- */
function renderOutro() {
    commitCurrent = null;
    prune();
    const list = visibleQuestions();

    // строка таблицы — тоже переход к вопросу: это самый очевидный способ
    // вернуться и что-то поправить
    let rows = '';
    list.forEach(function(q, i) {
        const vals = answerList(q.id);
        const cell = vals.length
            ? vals.map(function(v) { return '<span class="chip">' + esc(v) + '</span>'; }).join('')
            : '<span class="dash">—</span>';
        rows +=
            '<tr class="fade-item res-row" data-goto="' + esc(q.id) + '" ' +
                'style="animation-delay:' + (0.03 * i) + 's">' +
            '<td class="res-q"><span class="num">' + esc(q.num) + '</span>' + esc(q.text) + '</td>' +
            '<td class="res-a">' + cell + '</td>' +
            '<td class="res-go"><span class="res-go-label">изменить</span></td>' +
            '</tr>';
    });

    const result = checkCompat();
    const unchecked = uncheckedAnswers();

    let reportHtml = '<div class="compat-report fade-item stagger-2">';
    if (!result.hard.length && !result.soft.length) {
        reportHtml += '<div class="compat compat-ok">' +
            '<div class="compat-title">Противоречий не найдено</div>' +
            '<p class="compat-note">Сверили ' + result.pairs +
                ' пар ответов по таблице совместимости — всё сочетается.</p>' +
            '</div>';
    } else {
        reportHtml +=
            conflictBlockHtml(result.hard, Compat.HARD, null) +
            conflictBlockHtml(result.soft, Compat.SOFT, null);
    }
    if (unchecked.length) {
        reportHtml += '<p class="compat-skipped">Не проверялись по таблице (свой ответ ' +
            'или вариант, которого в ней нет): ' +
            unchecked.map(function(u) {
                return 'вопрос ' + esc(u.num) + ' — ' +
                    u.values.map(function(v) { return '«' + esc(v) + '»'; }).join(', ');
            }).join('; ') + '.</p>';
    }
    reportHtml += '</div>';

    screen.innerHTML =
        '<div class="fade-item stagger-1">' +
            '<span class="badge' + (result.hard.length ? '' : ' success') + '">' +
                (result.hard.length ? 'Есть противоречия' : 'Готово') +
            '</span>' +
            '<h2 class="result-title">Параметры вашей игры</h2>' +
            '<p class="subtitle">Ответы сохранены в этой сессии. Дальше — генерация концепта.</p>' +
        '</div>' +
        reportHtml +
        '<div class="result-table-wrap fade-item stagger-3">' +
            '<table class="result-table"><tbody>' + rows + '</tbody></table>' +
        '</div>' +
        '<div class="nav fade-item stagger-4">' +
            '<button class="btn ghost" id="repeatBtn" type="button">Пройти заново</button>' +
            '<button class="btn ghost" id="saveBtn" type="button">Сохранить ответы</button>' +
            '<button class="btn" id="genBtn" type="button">Сгенерировать механики</button>' +
        '</div>' +
        '<div class="gen-slot" id="genSlot"></div>';

    bindConflictJumps(screen);
    screen.querySelectorAll('.res-row').forEach(function(row) {
        row.onclick = function() { jumpTo(row.getAttribute('data-goto'), true); };
    });
    document.getElementById('repeatBtn').onclick = function() {
        resetState();
        go(STEP_INTRO);
    };
    document.getElementById('saveBtn').onclick = exportAnswers;
    document.getElementById('genBtn').onclick = function() {
        generateMechanics(result.hard.length);
    };

    updateProgress();
}

/* ---------- генерация механик (первый агент конвейера) ----------
   Кнопка зовёт наш сервер, а не модель напрямую: ключ OpenRouter не должен
   попадать в код страницы. Проверка по таблице совместимости стоит до вызова —
   иначе агент получит противоречивые параметры и попытки сгорят впустую. */

let generating = false;
let auditing = false;
let lastGeneration = null;   // ответ генератора: из него берётся выбранный вариант

function generateMechanics(hardConflicts) {
    if (generating) return;

    const slot = document.getElementById('genSlot');
    const btn = document.getElementById('genBtn');

    if (hardConflicts) {
        slot.innerHTML = '<div class="compat compat-hard">' +
            '<div class="compat-title">Сначала уберите противоречия</div>' +
            '<p class="compat-note">В ответах ' + hardConflicts +
                ' несовместимых сочетаний. Генератор честно попытается их выполнить, ' +
                'результат отклонит проверка, а попытки будут потрачены. ' +
                'Исправьте ответы выше и вернитесь сюда.</p>' +
            '</div>';
        return;
    }

    generating = true;
    btn.disabled = true;
    btn.textContent = 'Генерирую...';
    slot.innerHTML = '<div class="gen-wait">Собираю варианты игрового цикла. ' +
        'Это занимает до минуты.</div>';

    /* адрес ОТНОСИТЕЛЬНЫЙ: на сервере страница живёт под префиксом /generator/,
       и запрос по '/api/...' ушёл бы в корень домена — то есть в ФинИгроСкоп */
    fetch('api/generate/mechanics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers: answers })
    }).then(function(response) {
        return response.json().then(function(body) {
            return { status: response.status, body: body };
        });
    }).then(function(res) {
        if (res.body && res.body.ok) {
            lastGeneration = res.body;
            slot.innerHTML = mechanicsHtml(res.body) +
                '<div class="audit-slot" id="auditSlot"></div>';
            bindVariantPicks(slot);
        } else {
            lastGeneration = null;
            slot.innerHTML = genErrorHtml(res.body);
        }
    }).catch(function(e) {
        slot.innerHTML = genErrorHtml({ error: 'Сервер не ответил: ' + e.message });
    }).then(function() {
        generating = false;
        btn.disabled = false;
        btn.textContent = 'Сгенерировать механики';
    });
}

function genErrorHtml(body) {
    const stage = body && body.stage ? ' (этап: ' + esc(body.stage) + ')' : '';
    const problems = (body && body.problems) || [];
    return '<div class="compat compat-hard">' +
        '<div class="compat-title">Не получилось' + stage + '</div>' +
        '<p class="compat-note">' +
            esc((body && body.error) || 'Неизвестная ошибка.') + '</p>' +
        (problems.length
            ? '<ul class="conflict-list">' + problems.map(function(p) {
                  return '<li class="gen-problem">' + esc(p) + '</li>';
              }).join('') + '</ul>'
            : '') +
        '</div>';
}

function mechanicsHtml(body) {
    const data = body.data || {};
    const variants = data.variants || [];
    const recommended = data.recommended_variant_id;

    const invented = body.invented || [];
    const inventedIds = invented.map(function(m) { return m.id; });

    let html = '<div class="gen-head">' +
        '<h3 class="gen-title">Варианты игрового цикла</h3>' +
        '<p class="gen-note">Модель ' + esc(body.model || '') +
            ', попыток: ' + esc(body.attempts) +
            '. Механик из библиотеки: ' +
            esc((body.library_used || []).length) + '.</p>' +
        '</div>';

    // библиотека пока неполная: часть механик агент придумал сам —
    // об этом надо сказать прямо, а не выдавать их за библиотечные
    if (invented.length) {
        html += '<div class="compat compat-soft">' +
            '<div class="compat-title">Придумано агентом: ' + invented.length +
                ' механик</div>' +
            '<p class="compat-note">В библиотеке под эти параметры не хватило ' +
                'механик, поэтому агент достроил недостающие. Их стоит проверить ' +
                'и, если подходят, добавить в agents/library/mechanics.json.</p>' +
            '<ul class="conflict-list">' + invented.map(function(m) {
                return '<li class="gen-problem"><b>' + esc(m.id) + '</b> — ' +
                    esc(m.name) + '. ' + esc(m.description) +
                    ' Нужны: ' + esc((m.requires_components || []).join(', ') || '—') +
                    '</li>';
            }).join('') + '</ul>' +
            '</div>';
    }

    variants.forEach(function(v) {
        const best = v.variant_id === recommended;
        const loop = v.game_loop || {};
        const steps = loop.turn_structure || [];
        html += '<div class="variant' + (best ? ' best' : '') + '">' +
            '<div class="variant-head">' +
                '<span class="variant-title">' + esc(v.title || '') + '</span>' +
                (best ? '<span class="variant-badge">рекомендуем</span>' : '') +
            '</div>' +
            '<div class="variant-mech">' +
                (v.core_mechanics || []).map(function(m) {
                    const isNew = inventedIds.indexOf(m.id) !== -1;
                    return '<span class="chip' + (isNew ? ' chip-new' : '') + '">' +
                        esc(m.id) + ' — ' + esc(m.role) +
                        (isNew ? ' (придумана)' : '') + '</span>';
                }).join('') +
            '</div>' +
            (steps.length
                ? '<div class="variant-block"><b>Ход игрока:</b><ol>' +
                      steps.map(function(s) { return '<li>' + esc(s) + '</li>'; }).join('') +
                  '</ol></div>'
                : '') +
            variantRow('Передача хода', loop.turn_order) +
            variantRow('Победа', (v.win_condition || {}).trigger) +
            variantRow('Поражение', (v.lose_condition || {}).trigger) +
            variantRow('Поддержка отстающих', v.catch_up_mechanism) +
            variantRow('Роль случайности', v.randomness_role) +
            variantRow('Длительность', v.estimated_duration_minutes
                ? v.estimated_duration_minutes + ' мин' : null) +
            variantRow('Компоненты', (v.required_component_types || []).join(', ')) +
            variantRow('Почему подходит', v.fit_rationale) +
            ((v.risks || []).length
                ? '<div class="variant-block"><b>Риски:</b><ul>' +
                      v.risks.map(function(r) { return '<li>' + esc(r) + '</li>'; }).join('') +
                  '</ul></div>'
                : '') +
            '<div class="variant-actions">' +
                '<button class="btn small pick" type="button" ' +
                        'data-variant="' + esc(v.variant_id) + '">' +
                    'Выбрать и проверить аудитором' +
                '</button>' +
            '</div>' +
            '</div>';
    });

    if (data.recommendation_rationale) {
        html += '<p class="gen-note">Почему рекомендован этот вариант: ' +
            esc(data.recommendation_rationale) + '</p>';
    }
    if ((body.warnings || []).length) {
        html += '<div class="compat compat-soft">' +
            '<div class="compat-title">Замечания проверки</div>' +
            '<ul class="conflict-list">' + body.warnings.map(function(w) {
                return '<li class="gen-problem">' + esc(w) + '</li>';
            }).join('') + '</ul></div>';
    }
    return html;
}

function variantRow(label, value) {
    if (!value) return '';
    return '<div class="variant-block"><b>' + esc(label) + ':</b> ' + esc(value) + '</div>';
}

/* ---------- аудит выбранного варианта ----------
   Второй шаг конвейера. Генератор предложил три цикла, пользователь выбирает
   один — и только он идёт аудитору. Проверять все три значило бы платить
   втрое за варианты, которые всё равно не пойдут дальше. */

function bindVariantPicks(root) {
    root.querySelectorAll('.pick').forEach(function(btn) {
        btn.onclick = function() {
            auditVariant(Number(btn.getAttribute('data-variant')), btn);
        };
    });
}

function auditVariant(variantId, btn) {
    if (auditing || !lastGeneration) return;

    const variants = (lastGeneration.data || {}).variants || [];
    const chosen = variants.filter(function(v) { return v.variant_id === variantId; })[0];
    if (!chosen) return;

    const slot = document.getElementById('auditSlot');
    const buttons = Array.from(document.querySelectorAll('.pick'));

    // помечаем выбранный вариант и глушим остальные кнопки на время проверки
    document.querySelectorAll('.variant').forEach(function(v) {
        v.classList.remove('chosen');
    });
    btn.closest('.variant').classList.add('chosen');
    buttons.forEach(function(b) { b.disabled = true; });
    btn.textContent = 'Проверяю...';

    auditing = true;
    slot.innerHTML = '<div class="gen-wait">Аудитор сверяет вариант ' + esc(variantId) +
        ' с вашими ответами по чек-листу из 14 пунктов. Это занимает до минуты.</div>';
    slot.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    /* адрес относительный — см. пояснение у /api/generate/mechanics */
    fetch('api/audit/mechanics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers: answers, module: chosen })
    }).then(function(response) {
        return response.json().then(function(body) {
            return { status: response.status, body: body };
        });
    }).then(function(res) {
        const audit = res.body;
        if (!audit || !audit.map) {
            slot.innerHTML = genErrorHtml(audit);
            return;
        }
        slot.innerHTML = auditHtml(audit, variantId);
        // Третий шаг конвейера: модуль без критичных замечаний уходит на оценку
        // по линзам Шелла. Решение принимается по НАХОДКАМ, а не по вердикту
        // audit.passed: линзы стоят денег и минут, и запускать их по модулю,
        // который всё равно уйдёт на перегенерацию, незачем.
        runLenses(chosen, audit, variantId);
    }).catch(function(e) {
        slot.innerHTML = genErrorHtml({ error: 'Сервер не ответил: ' + e.message });
    }).then(function() {
        auditing = false;
        buttons.forEach(function(b) { b.disabled = false; });
        btn.textContent = 'Проверить ещё раз';
        buttons.filter(function(b) { return b !== btn; })
               .forEach(function(b) { b.textContent = 'Выбрать и проверить аудитором'; });
    });
}

const AUDIT_STATUS = {
    'ok': { mark: '✓', title: 'выполнено', cls: 'st-ok' },
    'concern': { mark: '!', title: 'замечание', cls: 'st-concern' },
    'violation': { mark: '✕', title: 'нарушение', cls: 'st-violation' },
    'n/a': { mark: '—', title: 'проверка неприменима', cls: 'st-na' }
};

function auditHtml(body, variantId) {
    const labels = body.labels || {};
    const rows = body.map || [];
    const counts = { ok: 0, concern: 0, violation: 0, 'n/a': 0 };
    rows.forEach(function(r) {
        if (counts[r.status] !== undefined) counts[r.status]++;
    });

    let html = '<div class="audit">' +
        '<div class="audit-head">' +
            '<span class="badge' + (body.passed ? ' success' : '') + '">' +
                (body.passed ? 'Модуль принят' : 'Модуль отклонён') +
            '</span>' +
            '<h3 class="gen-title">Аудит варианта ' + esc(variantId) + '</h3>' +
            '<p class="gen-note">' + esc(body.summary || '') + '</p>' +
            '<p class="gen-note">Пунктов проверено: ' + rows.length +
                ' — выполнено ' + counts.ok +
                ', замечаний ' + counts.concern +
                ', нарушений ' + counts.violation +
                ', неприменимо ' + counts['n/a'] +
                '. Модель ' + esc(body.model || '') +
                ', попыток ' + esc(body.attempts) + '.</p>' +
        '</div>';

    // карта проверки: показываем все статусы, включая n/a — по документу это
    // не то же самое, что «выполнено», и путать их нельзя
    html += '<ul class="audit-map">' + rows.map(function(r) {
        const st = AUDIT_STATUS[r.status] || AUDIT_STATUS['n/a'];
        return '<li class="audit-row ' + st.cls + '">' +
            '<span class="audit-mark" title="' + esc(st.title) + '">' + st.mark + '</span>' +
            '<span class="audit-body">' +
                '<span class="audit-item">' + esc(labels[r.item] || r.item) + '</span>' +
                (r.note ? '<span class="audit-note">' + esc(r.note) + '</span>' : '') +
            '</span>' +
        '</li>';
    }).join('') + '</ul>';

    if ((body.issues || []).length) {
        html += '<div class="audit-issues">' +
            '<div class="compat-title">Находки аудитора</div>' +
            '<ul class="conflict-list">' + body.issues.map(function(i) {
                return '<li class="gen-problem">' +
                    '<span class="sev sev-' + esc(i.severity) + '">' +
                        (i.severity === 'critical' ? 'критично' : 'некритично') +
                    '</span> ' +
                    '<b>' + esc(labels[i.checklist_item] || i.checklist_item) + '</b><br>' +
                    esc(i.explanation) +
                    (i.location_hint ? ' <i>(' + esc(i.location_hint) + ')</i>' : '') +
                '</li>';
            }).join('') + '</ul></div>';
    }

    if ((body.anomalies || []).length) {
        html += '<details class="audit-anomalies">' +
            '<summary>Служебное: расхождения в ответе модели (' +
                body.anomalies.length + ')</summary>' +
            '<ul>' + body.anomalies.map(function(a) {
                return '<li>' + esc(a) + '</li>';
            }).join('') + '</ul></details>';
    }

    html += '<p class="gen-note">' +
        (body.passed
            ? 'Аудит пройден. Следующий шаг — оценка по линзам Шелла.'
            : 'При таком результате оркестратор вернул бы находки генератору ' +
              'механик и запросил новый вариант.') +
        '</p>';

    // Место под оценку по линзам. Создаётся здесь, а не в index.html, чтобы
    // при повторной проверке варианта оно очищалось вместе с самим аудитом:
    // иначе рядом со свежим аудитом висел бы балл от прошлого прогона.
    html += '<div id="lensSlot"></div>';

    html += '</div>';

    return html;
}

/* ---------- оценка по линзам Шелла ---------- */
/* Агент живёт в ФинИгроСкопе: он владеет 47 линзами, весами категорий и шкалой.
   Здесь только показ результата — считать балл на странице нельзя, иначе у двух
   сервисов появятся две шкалы Шелла с одинаковым названием. */

function lensBlockers(audit) {
    const violations = (audit.map || []).filter(function(r) {
        return r.status === 'violation';
    });
    const critical = (audit.issues || []).filter(function(i) {
        return i.severity === 'critical';
    });
    return violations.length + critical.length;
}

function runLenses(module, audit, variantId) {
    const slot = document.getElementById('lensSlot');
    if (!slot) return;

    if (lensBlockers(audit)) {
        slot.innerHTML = '<div class="lens lens-skip">' +
            '<b>Линзы Шелла пропущены.</b> У аудитора остались критичные ' +
            'замечания — модуль сперва чинят, иначе оценка устареет в момент ' +
            'правки.</div>';
        return;
    }

    slot.innerHTML = '<div class="gen-wait">Оцениваю модуль по линзам Шелла. ' +
        'Агент живёт в ФинИгроСкопе, ответ занимает до нескольких минут.</div>';

    fetch('api/lenses/module', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            phase: 'mechanics',
            answers: answers,
            module: module,
            audit: audit
        })
    }).then(function(response) {
        return response.json().then(function(body) {
            return { status: response.status, body: body };
        });
    }).then(function(res) {
        slot.innerHTML = lensHtml(res.body, variantId);
    }).catch(function(e) {
        slot.innerHTML = genErrorHtml({ error: 'Сервер не ответил: ' + e.message });
    });
}

function lensHtml(body, variantId) {
    if (!body || body.error) {
        return genErrorHtml(body || { error: 'Пустой ответ' });
    }
    if (body.ready === false) {
        return '<div class="lens lens-skip"><b>Линзы Шелла пропущены.</b> ' +
            esc(body.reason || '') + '</div>';
    }
    if (body.available === false) {
        return genErrorHtml({ error: body.error || 'Модель не ответила' });
    }

    const score = body.score || {};
    const covered = Math.round((score.weight_covered || 0) * 100);
    const passed = score.passed;

    let html = '<div class="lens">' +
        '<div class="lens-head">' +
            '<span class="badge' + (passed ? ' success' : '') + '">' +
                (passed ? 'Порог пройден' : 'Ниже порога') +
            '</span>' +
            '<h3 class="gen-title">Линзы Шелла — вариант ' + esc(variantId) + '</h3>' +
            '<div class="lens-score' + (passed ? ' ok' : ' low') + '">' +
                esc(score.overall === null || score.overall === undefined
                    ? '—' : score.overall) +
                '<span class="lens-max"> / 10</span>' +
            '</div>' +
            '<p class="gen-note">Порог ' + esc(score.passing_score) +
                '. Оценено категорий ' + esc(score.categories_scored) +
                ' из ' + esc(score.categories_in_scope) + ' применимых, ' +
                'линз ' + esc((body.scope || {}).lenses ? body.scope.lenses.length : 0) +
                '.</p>' +
        '</div>';

    /* Главное, что нужно показать рядом с баллом: какую долю веса он охватывает.
       Без этого 8.4 за модуль механик неотличимы от 8.4 за игру целиком, хотя
       считаются по разному числу категорий. Формулой это не чинится — только
       показом. */
    html += '<div class="lens-cover">' +
        '<div class="lens-cover-bar"><span style="width:' + covered + '%"></span></div>' +
        '<p class="gen-note">Балл посчитан по <b>' + covered + '%</b> веса всех ' +
            'категорий: модуль механик физически не содержит материала для ' +
            'остальных. Это не пробел в оценке — остальное оценивается на ' +
            'своих этапах.</p>' +
    '</div>';

    html += '<table class="lens-table"><thead><tr>' +
        '<th>Категория</th><th>Вес</th><th>Балл</th></tr></thead><tbody>' +
        (score.rows || []).map(function(r) {
            const cls = r.in_scope ? '' : ' class="lens-out"';
            const value = (r.score === null || r.score === undefined)
                ? '<i>' + esc(r.na_reason || 'не оценена') + '</i>'
                : esc(r.score);
            return '<tr' + cls + '><td>' + esc(r.category) + '</td>' +
                '<td>' + esc(r.weight) + '</td><td>' + value + '</td></tr>';
        }).join('') + '</tbody></table>';

    const findings = (body.report || {}).findings || [];
    if (findings.length) {
        html += '<div class="audit-issues">' +
            '<div class="compat-title">Находки по линзам (' + findings.length + ')</div>' +
            '<ul class="conflict-list">' + findings.map(function(f) {
                return '<li class="gen-problem">' +
                    (f.severity ? '<span class="sev sev-' + esc(f.severity) + '">' +
                        esc(f.severity) + '</span> ' : '') +
                    (f.lens ? '<b>Линза ' + esc(f.lens) + '</b><br>' : '') +
                    esc(f.detail || f.text || '') +
                '</li>';
            }).join('') + '</ul></div>';
    }

    if ((body.issues || []).length) {
        html += '<details class="audit-anomalies">' +
            '<summary>Служебное: замечания к ответу агента (' +
                body.issues.length + ')</summary><ul>' +
            body.issues.map(function(i) {
                return '<li>' + esc(i.code) + ': ' + esc(i.message) + '</li>';
            }).join('') + '</ul></details>';
    }

    html += '<p class="gen-note">' + (passed
        ? 'Модуль принят и по чек-листу, и по линзам — дальше по конвейеру ' +
          'он ушёл бы на генерацию сюжета.'
        : 'Балл ниже порога: оркестратор запросил бы новый вариант механик.') +
        '</p></div>';

    return html;
}

function exportAnswers() {
    let text = 'ИТОГИ ОПРОСА — ГЕНЕРАТОР ИГР\n========================================\n';
    let block = '';
    visibleQuestions().forEach(function(q) {
        if (q.block !== block) {
            block = q.block;
            text += '\n--- ' + block + ' ---\n';
        }
        const vals = answerList(q.id);
        text += q.num + '. ' + q.text + '\n   ' + (vals.length ? vals.join('; ') : '—') + '\n';
    });

    const result = checkCompat();
    text += '\n--- Проверка совместимости (' + Compat.source + ') ---\n';
    text += 'Сверено пар ответов: ' + result.pairs + '\n';

    function line(c) {
        return '- «' + c.a.option + '» (вопрос ' + c.a.num + ') + «' +
            c.b.option + '» (вопрос ' + c.b.num + ')\n';
    }
    if (result.hard.length) {
        text += '\nНесовместимо (в таблице «-»):\n';
        result.hard.forEach(function(c) { text += line(c); });
    }
    if (result.soft.length) {
        text += '\nСпорные сочетания (в таблице «+/-»):\n';
        result.soft.forEach(function(c) { text += line(c); });
    }
    if (!result.hard.length && !result.soft.length) {
        text += 'Противоречий не найдено.\n';
    }

    const unchecked = uncheckedAnswers();
    if (unchecked.length) {
        text += '\nНе проверялось по таблице (свой ответ или вариант, которого в ней нет):\n';
        unchecked.forEach(function(u) {
            text += '- вопрос ' + u.num + ': ' + u.values.join('; ') + '\n';
        });
    }

    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'game_survey_results.txt';
    a.click();
    URL.revokeObjectURL(a.href);
}

/* ---------- навигация с учётом скрытых вопросов ---------- */
function firstStep() {
    for (let i = 0; i < flat.length; i++) {
        if (isVisible(flat[i])) return STEP_BASE + i;
    }
    return STEP_OUTRO;
}

function goNext(idx) {
    for (let i = idx + 1; i < flat.length; i++) {
        if (isVisible(flat[i])) { go(STEP_BASE + i); return; }
    }
    go(STEP_OUTRO);
}

function goPrev(idx) {
    for (let i = idx - 1; i >= 0; i--) {
        if (isVisible(flat[i])) { go(STEP_BASE + i); return; }
    }
    go(STEP_INTRO);
}

function go(step) {
    // дошли туда, откуда уходили по кнопке — возвращать больше некуда
    if (step === returnTo) returnTo = null;
    currentStep = step;
    saveState();
    transition(function() { render(); });
}

function render() {
    if (currentStep === STEP_INTRO) { renderIntro(); return; }
    if (currentStep === STEP_OUTRO) { renderOutro(); return; }

    const idx = currentStep - STEP_BASE;
    // вопрос мог стать невидимым после правки предыдущего ответа
    if (idx < 0 || idx >= flat.length || !isVisible(flat[idx])) {
        currentStep = firstStep();
        saveState();
        if (currentStep === STEP_OUTRO) { renderOutro(); return; }
        renderQuestion(currentStep - STEP_BASE);
        return;
    }
    renderQuestion(idx);
}

restartMain.onclick = function() {
    if (confirm('Начать опрос заново? Текущие ответы будут удалены.')) {
        resetState();
        go(STEP_INTRO);
    }
};

/* Кнопка перехода в соседний сервис. Адрес знает только сервер (FINIGROSKOP_URL
   в .env): на сервере это не localhost. Пока ответа нет — кнопки нет; если
   сосед не настроен, опросник от этого не страдает и ошибку не показывает. */
fetch('api/links')
    .then(function(response) { return response.ok ? response.json() : null; })
    .then(function(data) {
        if (!data || !data.finigroskop) return;
        linkFinigroskop.href = data.finigroskop;
        linkFinigroskop.hidden = false;
    })
    .catch(function() { /* адрес неизвестен — показывать нечего */ });

/* ---------- старт ---------- */
loadState();
render();
