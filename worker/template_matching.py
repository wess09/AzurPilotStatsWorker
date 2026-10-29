"""Keep native sliding NCC while preferring formal over temporary templates."""


def install_priorities(grid, temporary, formal_similarity=None):
    import cv2
    import numpy as np
    from module.base.utils import crop, color_similar, lower_template_match_similarity

    native_threshold = grid.template_similarity_for

    def threshold(name, similarity):
        value = native_threshold(name, formal_similarity if formal_similarity is not None else similarity)
        return max(value, temporary[name]) if name in temporary else value

    grid.template_similarity_for = threshold
    if not temporary:
        return

    def match(image, similarity=None):
        similarity = lower_template_match_similarity(grid.similarity if similarity is None else similarity)
        color = cv2.mean(crop(image, grid.template_area))[:3]
        names = sorted(grid.templates, key=lambda name: -grid.templates_hit[name])
        names, similarity = grid.match_candidates(image, names, similarity)
        # Search every candidate within its tier, then stop at the first tier
        # with a confirmed match. Numerical temporary templates stay last.
        tiers = ([name for name in names if not name.isdigit() and name not in temporary],
                 [name for name in names if name in temporary],
                 [name for name in names if name.isdigit()])
        for candidates in tiers:
            best_name, best_score = None, -1
            for name in candidates:
                if not color_similar(color1=color, color2=grid.colors[name], threshold=30):
                    continue
                response = cv2.matchTemplate(image, grid.templates[name], cv2.TM_CCOEFF_NORMED)
                score = cv2.minMaxLoc(response)[1]
                if score > grid.template_similarity_for(name, similarity) and score > best_score:
                    best_name, best_score = name, score
            if best_name is not None:
                grid.templates_hit[best_name] += 1
                return best_name
        grid.next_template_index += 1
        name = str(grid.next_template_index)
        icon = crop(image, grid.template_area)
        grid.templates[name] = icon
        grid.colors[name] = cv2.mean(icon)[:3]
        grid.templates_hit[name] = 0
        return name

    grid.match_template = match
